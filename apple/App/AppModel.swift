// What the host app does (docs/design.md §C–§E):
// * NSFileProviderManager.add(domain): one domain per macOS user. Identifier = the
//   gateway ROOT id, which is stable per (NAS user, share, path), so signing in again
//   after a password change keeps the domain and nothing is downloaded twice.
// * Route selection: probe on start, on NWPathMonitor changes and every 5 min.
// * Long-poll /v1/changes?wait=25 and signalEnumerator(for: .workingSet).
// * Token rotation (weekly) — the ONLY writer of the Keychain item.
// * Eviction (docs/design.md §E.1): only items the gateway acknowledged (extension journal),
//   after evict_after; the system refuses pinned ("Keep Downloaded") items itself.
import SwiftUI
import FileProvider
import Network
import ServiceManagement
import os
import LoftCore

let appLog = Logger(subsystem: "no.incremental.loft", category: "app")

@MainActor
final class MenuModel: ObservableObject {
    static let shared = MenuModel()

    let env = UITestMode.isOn
        ? LoftEnvironment(shared: UITestMode.freshState(), tokens: MemoryTokenStore(), legacy: nil)
        : LoftEnvironment()
    var client: GatewayClient { env.client }

    /// releases.json (docs/releases-json.md): the DMG isn't updated by the App Store. Off in UI
    /// tests and with LOFT_NO_UPDATE_CHECK=1, so no test or CI run ever asks the real site.
    let updates: UpdateChecker? = UITestMode.isOn || ProcessInfo.processInfo.environment["LOFT_NO_UPDATE_CHECK"] == "1"
        ? nil
        : UpdateChecker(currentVersion: Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0")
    /// A newer Loft for Mac: the menu shows "Loft x.y.z is available…".
    @Published private(set) var availableUpdate: AvailableRelease?
    @Published private(set) var updateCheckOn = true
    var shared: SharedState { env.shared }

    // MARK: published state

    /// Set by the sync loop. An active `error` outranks it (error > offline > syncing;
    /// the loop itself reports offline over syncing).
    @Published var status: SyncStatus = .upToDate(.direct) {
        didSet { updateSpinner() }
    }
    /// Set from the loop's failures. Cleared after a successful sign-in or a round that gets through.
    @Published var error: SyncError? {
        didSet { updateSpinner() }
    }
    @Published var pendingUploads = 0
    @Published var route: Route = .publicInternet
    @Published var lastSynced: Date?
    @Published private(set) var frame = 0
    @Published var settingsTab: SettingsTab = .account
    @Published private(set) var isSetUp = false
    @Published var paused = false
    @Published var nasUser: String?
    @Published var deviceName: String?
    @Published var rootPath: String?
    @Published var otherDevices: [Device] = []
    @Published var openAtLogin = SMAppService.mainApp.status == .enabled
    /// The gateway this Mac is paired with (docs/pairing.md); nil until paired.
    @Published private(set) var pairing: Pairing?

    private var spinner: Timer?
    private var loop: Task<Void, Never>?
    private var timers: [Timer] = []
    private let pathMonitor = NWPathMonitor()
    private var networkUp = true
    private var loopState: LoopState = .ok
    /// The extension's last "server unreachable" note this app has answered. Starts below
    /// any note (even "none"), so the first good round after launch always tells the system
    /// home is back: a banner raised before this launch, or by a build without notes, clears.
    private var handledUnreachable: Double = -1

    var shown: SyncStatus { error.map { .error($0, waiting: pendingUploads) } ?? status }
    var isPaused: Bool { paused }

    /// Notice on the sign-in sheet when "Sign in…" is opened away from home: password
    /// sign-in only works on a pinned route (RouteSelector.canPasswordSignIn).
    var signInNotice: String? {
        route == .direct || pairing?.publicAllowsSignIn == true ? nil : Copy.notAtHome
    }

    /// Asset-catalog template images (Assets.xcassets, 18 pt vector PDFs).
    var glyph: String {
        guard isSetUp else { return "loft-status-offline" }
        switch shown {
        case .upToDate, .paused: return "loft-status-idle"
        case .syncing: return "loft-status-syncing-frame\(frame)"
        case .offline, .cantReachHome, .cantReachNAS: return "loft-status-offline"
        case .error: return "loft-status-error"
        }
    }

    // 8 frames x 22.5° is one full spin of the badge arrows; ~8 fps loops seamlessly.
    private func updateSpinner() {
        guard case .syncing = shown else {
            spinner?.invalidate(); spinner = nil; frame = 0
            return
        }
        guard spinner == nil else { return }
        spinner = Timer.scheduledTimer(withTimeInterval: 1.0 / 8, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { if let self { self.frame = (self.frame + 1) % 8 } }
        }
    }

    // MARK: start-up

    func start() {
        nasUser = shared.nasUser
        deviceName = shared.deviceName
        rootPath = shared.root.map(Copy.rootName)
        paused = shared.paused
        lastSynced = shared.lastSynced
        pairing = env.pairing          // 0.1.x installs were migrated by LoftEnvironment
        isSetUp = pairing != nil && env.tokens.load() != nil && shared.root != nil

        pathMonitor.pathUpdateHandler = { [weak self] path in
            Task { @MainActor in
                guard let self else { return }
                self.networkUp = path.status == .satisfied
                await self.reprobe()
            }
        }
        pathMonitor.start(queue: .main)

        updateCheckOn = updates?.isEnabled ?? false
        availableUpdate = updates?.available

        timers = [
            // The checker itself keeps to once a day; the hourly tick only notices the day is up.
            Timer.scheduledTimer(withTimeInterval: 3600, repeats: true) { [weak self] _ in
                Task { @MainActor in await self?.checkForUpdate() }
            },
            Timer.scheduledTimer(withTimeInterval: 300, repeats: true) { [weak self] _ in
                Task { @MainActor in await self?.reprobe() }
            },
            Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in
                Task { @MainActor in await self?.refreshCounts() }
            },
            Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in
                Task { @MainActor in await self?.evictDue() }
            },
        ]

        Task { await checkForUpdate() }
        Task {
            await env.refreshPins()
            await reprobe()
            if isSetUp {
                await ensureDomain()
                startLoop()
            } else if pendingPairing == nil {    // a pairing link that launched us shows itself
                showOnboarding()
            }
        }
    }

    func reprobe() async {
        route = await env.reprobe()
        appLog.info("route \(self.route.rawValue, privacy: .public)")
    }

    // MARK: domain

    // MARK: update check

    func checkForUpdate() async {
        guard let u = updates else { return }
        let r = await u.checkIfDue()
        if let e = u.lastError { appLog.info("update check: \(e, privacy: .public)") }
        availableUpdate = r
    }

    func setUpdateCheck(_ on: Bool) {
        guard let u = updates else { return }
        u.isEnabled = on
        updateCheckOn = on
        availableUpdate = u.available
        if on { Task { await checkForUpdate() } }
    }

    func openUpdate() {
        guard let u = availableUpdate?.url, u.scheme == "https" else { return }
        NSWorkspace.shared.open(u)
    }

    private func domain(for root: RootInfo) -> NSFileProviderDomain {
        NSFileProviderDomain(identifier: NSFileProviderDomainIdentifier(rawValue: root.id),
                             displayName: Copy.domainName)
    }

    var manager: NSFileProviderManager? {
        guard !UITestMode.isOn, let root = shared.root else { return nil }
        return NSFileProviderManager(for: domain(for: root))
    }

    /// Exactly one Loft domain: the current root's. Others (an old folder, a stale
    /// install) are removed with their unsynced edits preserved.
    func ensureDomain() async {
        guard !UITestMode.isOn, let root = shared.root else { return }
        do {
            let want = domain(for: root)
            let have = try await Self.allDomains()
            for d in have where d.identifier != want.identifier {
                await removeDomain(d)
            }
            if !have.contains(where: { $0.identifier == want.identifier }) {
                try await NSFileProviderManager.add(want)
                appLog.info("domain added \(root.id, privacy: .public)")
            }
            shared.domainIdentifier = root.id
            if paused { try? await manager?.disconnect(reason: Copy.pausedReason, options: []) }
        } catch {
            appLog.error("domain: \(String(describing: error), privacy: .public)")
            self.error = .stopped(code: "domain_add")
        }
    }

    private static func allDomains() async throws -> [NSFileProviderDomain] {
        try await withCheckedThrowingContinuation { cont in
            NSFileProviderManager.getDomainsWithCompletionHandler { domains, error in
                if let error { cont.resume(throwing: error) } else { cont.resume(returning: domains) }
            }
        }
    }

    private func removeDomain(_ d: NSFileProviderDomain) async {
        guard !UITestMode.isOn else { return }
        do {
            let kept = try await NSFileProviderManager.remove(d, mode: .preserveDirtyUserData)
            appLog.info("domain removed \(d.identifier.rawValue, privacy: .public); unsynced kept at \(kept?.path ?? "-", privacy: .public)")
        } catch {
            appLog.error("domain remove: \(String(describing: error), privacy: .public)")
        }
    }

    // MARK: change watcher

    func startLoop() {
        loop?.cancel()
        guard isSetUp, !paused else { return }
        let watcher = ChangeWatcher(client: client)
        loop = Task { [weak self] in
            // Rotate first: the loop is the app's heartbeat, so a weekly rotation
            // happens whenever the Mac is on.
            await self?.rotateIfDue()
            await watcher.run(from: nil, onChange: { [weak self] in
                await self?.signalChanges()
            }, onState: { [weak self] st in
                await self?.loopRound(st)
            })
        }
    }

    func stopLoop() { loop?.cancel(); loop = nil }

    private func signalChanges() async {
        do { try await manager?.signalEnumerator(for: .workingSet) } catch {
            appLog.error("signal: \(String(describing: error), privacy: .public)")
        }
    }

    private func rotateIfDue() async {
        guard let t = env.tokens.load(), t.dueForRotation else { return }
        do { try await client.rotateToken(); appLog.info("token rotated") } catch {
            appLog.error("rotate: \(String(describing: error), privacy: .public)")
            await loopRound(LoopState(error))
        }
    }

    private func loopRound(_ st: LoopState) async {
        let was = loopState
        loopState = st
        switch st {
        case .ok:
            if error != nil { error = nil }
            lastSynced = Date()
            shared.lastSynced = lastSynced
            // The system backs off after .serverUnreachable until told home is back. The
            // extension's request may have failed while this loop never did (a gateway
            // restart while the long-poll was open), so ask for its note as well.
            let reported = shared.unreachableReportedAt
            if reported > handledUnreachable || was != .ok {
                handledUnreachable = reported
                try? await manager?.signalErrorResolved(NSFileProviderError(.serverUnreachable))
                await signalChanges()
            }
            await rotateIfDue()
        case .cantReachHome:
            await reprobe()
        case .cantReachNAS:
            break
        case .failed(let e):
            appLog.error("sync stopped: \(String(describing: e), privacy: .public)")
            error = e
        }
        await refreshCounts()
    }

    // MARK: counts + status line

    func refreshCounts() async {
        guard isSetUp else { return }
        let pending = await pendingCount()
        let active = shared.activeTransfers
        if pendingUploads != pending { pendingUploads = pending }
        let next: SyncStatus
        if paused {
            next = .paused(waiting: pending)
        } else {
            switch loopState {
            case .cantReachHome: next = networkUp ? .cantReachHome : .offline(waiting: pending)
            case .cantReachNAS: next = .cantReachNAS
            case .ok, .failed:
                let n = max(active, pending)
                next = n > 0 ? .syncing(files: n, route) : .upToDate(route)
            }
        }
        status = next
    }

    /// Items the system still has to hand to the extension (or that failed and wait
    /// for a retry): "N changes waiting to upload".
    private var counting = false

    private func pendingCount() async -> Int {
        guard let mgr = manager, !counting else { return pendingUploads }
        counting = true
        defer { counting = false }
        let en = mgr.enumeratorForPendingItems()
        defer { en.invalidate() }
        return await withCheckedContinuation { cont in
            let c = PendingCounter { cont.resume(returning: $0) }
            pendingObserver = c                       // the enumerator may hold it weakly
            en.enumerateItems(for: c, startingAt: NSFileProviderPage(NSFileProviderPage.initialPageSortedByName as Data))
            // Never let a silent enumerator wedge the status line.
            DispatchQueue.main.asyncAfter(deadline: .now() + 10) { c.finish() }
        }
    }
    private var pendingObserver: PendingCounter?

    // MARK: eviction (docs/design.md §E.1)

    private var evicting = false

    func evictDue() async {
        guard isSetUp, !evicting, let mgr = manager else { return }
        evicting = true
        defer { evicting = false }
        let after = TimeInterval(shared.evictAfterMinutes * 60)
        let now = Date()
        for (id, ack) in shared.uploadedJournal() where now.timeIntervalSince(ack) >= after {
            let ident = NSFileProviderItemIdentifier(id)
            // Recently opened: leave it until it has been idle for evict_after too.
            if let url = try? await mgr.getUserVisibleURL(for: ident),
               let used = try? url.resourceValues(forKeys: [.contentAccessDateKey]).contentAccessDate,
               now.timeIntervalSince(used) < after {
                continue
            }
            do {
                try await mgr.evictItem(identifier: ident)
                shared.removeFromJournal(id)
                appLog.info("evicted \(id, privacy: .public)")
            } catch let e as NSFileProviderError where [.noSuchItem, .nonEvictable].contains(e.code) {
                // Gone, or kept downloaded by the user. Logged: a nonEvictable here is also
                // what a wrong content policy looks like, and it used to vanish silently.
                appLog.info("not evicted \(id, privacy: .public): \(e.code.rawValue)")
                shared.removeFromJournal(id)
            } catch {
                // open, or edited again and not uploaded yet: next round. Give up after a day.
                if now.timeIntervalSince(ack) > 86400 { shared.removeFromJournal(id) }
            }
        }
    }

    // MARK: menu actions

    func openInFinder() {
        Task {
            guard let mgr = manager,
                  let url = try? await mgr.getUserVisibleURL(for: .rootContainer) else { return }
            NSWorkspace.shared.open(url)
        }
    }

    /// Opens the files in a loft://stream link in VLC (the right-click item).
    func streamInVLC(_ link: URL) {
        guard let ids = StreamLink.items(from: link) else {
            appLog.error("stream: not a stream link")
            return
        }
        Task {
            guard let mgr = manager else { return }
            var files: [URL] = []
            for id in ids {
                do { files.append(try await mgr.getUserVisibleURL(for: NSFileProviderItemIdentifier(id))) }
                catch { appLog.error("stream: \(id, privacy: .public): \(String(describing: error), privacy: .public)") }
            }
            openInVLC(files)
        }
    }

    /// Opens files in VLC the way dragging them onto VLC does: VLC reads them itself, in
    /// parts, so a Loft file streams instead of downloading first.
    func openInVLC(_ files: [URL]) {
        guard !files.isEmpty else { return }
        guard let vlc = NSWorkspace.shared.urlForApplication(withBundleIdentifier: StreamLink.vlcBundleId) else {
            showVLCMissing()
            return
        }
        Task {
            do {
                _ = try await NSWorkspace.shared.open(files, withApplicationAt: vlc,
                                                      configuration: NSWorkspace.OpenConfiguration())
                appLog.info("stream: \(files.count) file(s) to VLC")
            } catch {
                appLog.error("stream: VLC: \(String(describing: error), privacy: .public)")
            }
        }
    }

    private func showVLCMissing() {
        let a = NSAlert()
        a.messageText = Copy.vlcMissingTitle
        a.informativeText = Copy.vlcMissingBody
        a.addButton(withTitle: Copy.getVLC)
        a.addButton(withTitle: Copy.cancel)
        NSApp.activate()
        if a.runModal() == .alertFirstButtonReturn, let u = URL(string: "https://www.videolan.org/vlc/") {
            NSWorkspace.shared.open(u)
        }
    }

    func togglePause() {
        Task {
            guard let mgr = manager else { return }
            do {
                if paused {
                    try await mgr.reconnect()
                    paused = false
                    shared.paused = false
                    startLoop()
                } else {
                    try await mgr.disconnect(reason: Copy.pausedReason, options: [])
                    paused = true
                    shared.paused = true
                    stopLoop()
                }
            } catch {
                appLog.error("pause/resume: \(String(describing: error), privacy: .public)")
            }
            await refreshCounts()
        }
    }

    /// "Sign in…" opens the sign-in step; "Try again" clears the error and restarts the
    /// loop. ("Choose folder…" goes to Settings › Folder, see ErrorActionItem.)
    func errorAction() {
        guard let e = error else { return }
        if e.needsSignIn {
            showOnboarding(step: .signIn)
        } else {
            error = nil
            startLoop()
        }
    }

    // MARK: windows

    func showOnboarding(step: OnboardingStep? = nil, message: String? = nil) {
        let start = step ?? (pairing == nil || env.tokens.load() == nil ? .welcome : .signIn)
        let flow = OnboardingFlow(model: self, start: start)
        flow.message = message
        Windows.shared.show("onboarding", width: 640) { OnboardingView(flow: flow) }
    }

    // MARK: pairing (docs/pairing.md)

    /// A pairing the user hasn't confirmed yet (from a link, or pasted / typed).
    @Published var pendingPairing: Pairing?

    /// loft://pair#… or a universal link https://loft.incremental.no/pair#…: show what
    /// it is and ask before anything changes.
    func openPairingLink(_ url: URL) {
        do {
            pendingPairing = try Pairing.parse(url: url)
            showOnboarding(step: .confirmPair)
        } catch {
            appLog.error("pairing link: \(String(describing: error), privacy: .public)")
            pendingPairing = nil
            showOnboarding(step: .pair, message: Copy.pairingError(error))
        }
    }

    /// The pairing shares a key with the current one: the same gateway (new address or
    /// code), so the sign-in carries over.
    func isSameGateway(_ p: Pairing) -> Bool {
        guard pairing != nil else { return false }
        return !shared.effectivePins.isDisjoint(with: p.pins)
    }

    /// Pair with `p` (one gateway per Mac). Another gateway signs this Mac out of the old
    /// one first: its token, device row and Finder location belong there.
    func pair(_ p: Pairing) async {
        if !isSameGateway(p), env.tokens.load() != nil || shared.root != nil {
            await forgetAccount()
        }
        await env.pair(p)
        pairing = env.pairing
        pendingPairing = nil
        appLog.info("paired (\(p.publicMode?.rawValue ?? "home only", privacy: .public))")
        await reprobe()
        await env.refreshPins()
    }

    func showSettings(_ tab: SettingsTab) {
        settingsTab = tab
        Windows.shared.show("settings", width: 480) { SettingsView(model: self) }
        Task { await loadDevices() }
    }

    // MARK: account

    enum SignInFailure: Error { case message(String) }

    /// Password -> device token (pinned routes only). The password is not kept.
    func signIn(user: String, password: String) async throws {
        guard pairing != nil else { throw SignInFailure.message(Copy.notPaired) }
        await reprobe()
        guard env.selector.canPasswordSignIn else { throw SignInFailure.message(Copy.notAtHome) }
        let name = Host.current().localizedName ?? "Mac"
        let r: LoginResult
        do {
            r = try await client.login(user: user.trimmingCharacters(in: .whitespaces),
                                       password: password, deviceName: name)
        } catch GatewayError.api(let e) {
            appLog.error("sign-in: \(e.status) \(e.error, privacy: .public)")
            switch e.error {
            case "invalid_credentials", "missing_credentials": throw SignInFailure.message(Copy.wrongPassword)
            case "account_not_allowed": throw SignInFailure.message(Copy.accountNotAllowed)
            case "too_many_attempts": throw SignInFailure.message(Copy.tooManyAttempts)
            case "password_login_direct_only": throw SignInFailure.message(Copy.notAtHome)
            case "nas_unavailable": throw SignInFailure.message(Copy.cantReachNAS)
            default: throw SignInFailure.message(Copy.signInFailed)
            }
        } catch {
            appLog.error("sign-in: \(String(describing: error), privacy: .public)")
            throw SignInFailure.message(Copy.notAtHome)
        }
        // A different account than before: the old domain and root belong to it.
        if let old = shared.nasUser, old.caseInsensitiveCompare(r.nasUser ?? user) != .orderedSame {
            if let root = shared.root { await removeDomain(domain(for: root)) }
            shared.root = nil
        }
        if let old = env.tokens.load(), old.deviceId != r.deviceId {
            try? await client.revoke(deviceId: old.deviceId)   // the dead device row; best effort
        }
        try env.tokens.save(StoredToken(r))
        shared.nasUser = r.nasUser ?? user
        shared.deviceId = r.deviceId
        shared.deviceName = name
        nasUser = shared.nasUser
        deviceName = name
        error = nil
    }

    /// Same folder as before a re-sign-in: pick it again on the new device row, which
    /// gives the same root id, so the Finder location simply carries on.
    func restoreRootIfKnown() async -> Bool {
        guard let root = shared.root else { return false }
        do {
            try await useFolder(share: root.share, path: root.path)
            return true
        } catch {
            return false
        }
    }

    func useFolder(share: String, path: String) async throws {
        let me = try await client.setRoot(share: share, path: path)
        guard let root = me.root else { throw SignInFailure.message(Copy.signInFailed) }
        shared.root = root
        rootPath = Copy.rootName(root)
        isSetUp = true
        await ensureDomain()
        try? await manager?.signalErrorResolved(NSFileProviderError(.notAuthenticated))
        await signalChanges()
        startLoop()
    }

    func signOut() async {
        await forgetAccount()
        Windows.shared.close("settings")
        showOnboarding(step: .welcome)
    }

    /// Sign-out without the windows: revoke this device, drop the Finder location (unsynced
    /// edits preserved), the token and the account state. The pairing stays.
    private func forgetAccount() async {
        stopLoop()
        if let id = env.tokens.load()?.deviceId { try? await client.revoke(deviceId: id) }
        if let root = shared.root { await removeDomain(domain(for: root)) }
        env.tokens.clear()
        shared.clearAccount()
        isSetUp = false
        nasUser = nil; deviceName = nil; rootPath = nil; error = nil; paused = false
        otherDevices = []
    }

    func loadDevices() async {
        guard isSetUp, let r = try? await client.devices() else { return }
        otherDevices = r.devices.filter { $0.id != r.current && $0.revokedAt == nil }
    }

    func setOpenAtLogin(_ on: Bool) {
        do {
            if on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
        } catch {
            appLog.error("login item: \(String(describing: error), privacy: .public)")
        }
        openAtLogin = SMAppService.mainApp.status == .enabled
    }
}

/// Counts the pending set; one page is plenty for a menu line. Callbacks arrive on any
/// queue, and `finish` may also be called by the timeout, so it reports exactly once.
final class PendingCounter: NSObject, NSFileProviderEnumerationObserver, @unchecked Sendable {
    private var count = 0
    private var done: ((Int) -> Void)?
    private let lock = NSLock()
    init(_ done: @escaping (Int) -> Void) { self.done = done }

    func didEnumerate(_ updatedItems: [NSFileProviderItemProtocol]) {
        lock.withLock { count += updatedItems.count }
    }
    func finishEnumerating(upTo nextPage: NSFileProviderPage?) { finish() }
    func finishEnumeratingWithError(_ error: Error) { finish() }
    func finish() {
        let (d, n): (((Int) -> Void)?, Int) = lock.withLock { let d = done; done = nil; return (d, count) }
        d?(n)
    }
}

/// `-LoftUITest` (Debug builds only; LoftMacUITests passes it): the app runs beside an
/// installed Loft without touching it. Its own defaults suite, emptied at launch, a token kept
/// in memory instead of the Keychain, no File Provider domain, no update check, no 0.1.x
/// migration. A Release build has no such switch.
enum UITestMode {
    #if DEBUG
    static let isOn = ProcessInfo.processInfo.arguments.contains("-LoftUITest")
    #else
    static let isOn = false
    #endif
    static let suite = "no.incremental.loft.uitest"

    static func freshState() -> SharedState {
        UserDefaults.standard.removePersistentDomain(forName: suite)
        return SharedState(suiteName: suite)
    }
}
