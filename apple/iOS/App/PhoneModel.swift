// App state: sign-in (password on the home network, or a sign-in code), the all-shares
// root, the Files location (one File Provider domain), photo uploads.
import FileProvider
import Foundation
import LoftCore
import os
import UIKit

let appLog = Logger(subsystem: "no.incremental.loft", category: "app")

@MainActor
final class PhoneModel: ObservableObject {
    let env = LoftEnvironment()
    var client: GatewayClient { env.client }
    var shared: SharedState { env.shared }

    @Published var started = false
    @Published var rootItemId: String?
    @Published var nasUser: String?
    @Published var route: Route = .publicInternet
    @Published var uploads: [UploadJob] = []
    @Published var signedOutReason: String?
    /// The gateway this iPhone is paired with (docs/pairing.md); nil until paired.
    @Published var pairing: Pairing?
    /// A pairing waiting for the user's yes (from a link, the QR code, or typed).
    @Published var pendingPairing: Pairing?
    /// "Pair with another gateway": the pairing screen over a working setup.
    @Published var choosingGateway = false

    // MARK: start

    func start() async {
        nasUser = shared.nasUser
        pairing = env.pairing          // 0.1.x installs were migrated by LoftEnvironment
        if pairing != nil, env.tokens.load() != nil, let root = shared.root {
            rootItemId = root.rootItemId
        }
        started = true
        await env.refreshPins()
        route = await env.reprobe()
        if rootItemId != nil {
            await ensureDomain()
            await checkStillSignedIn()
        }
    }

    func becameActive() async {
        guard started else { return }
        route = await env.reprobe()
        guard rootItemId != nil else { return }
        if let t = env.tokens.load(), t.dueForRotation { _ = try? await client.rotateToken() }
        try? await domainManager?.signalEnumerator(for: .workingSet)
    }

    /// A revoked device or a changed NAS password signs the phone out, with the reason.
    private func checkStillSignedIn() async {
        do { _ = try await client.me() } catch GatewayError.api(let e) where e.needsSignIn {
            await forgetAccount()
            signedOutReason = e.error == "device_revoked" ? PhoneCopy.deviceRemoved : PhoneCopy.signInExpired
        } catch {}
    }

    // MARK: sign-in

    enum SignInFailure: Error { case message(String) }

    var deviceName: String { UIDevice.current.name }

    // MARK: pairing (docs/pairing.md)

    /// From the universal link, the QR code, or a typed/pasted link. Throws the message to show.
    func offerPairing(_ text: String) throws {
        do { pendingPairing = try Pairing.parse(text) } catch {
            appLog.error("pairing: \(String(describing: error), privacy: .public)")
            throw SignInFailure.message(PhoneCopy.pairingError(error))
        }
    }

    func offerManualPairing(address: String, pin: String) throws {
        do { pendingPairing = try Pairing.manual(address: address, pins: pin) } catch {
            throw SignInFailure.message(PhoneCopy.pairingError(error, manual: true))
        }
    }

    /// The universal link https://loft.incremental.no/pair#… opened the app.
    func openPairingLink(_ url: URL) {
        do { try offerPairing(url.absoluteString) } catch SignInFailure.message(let m) {
            pairingLinkError = m
        } catch {}
    }
    @Published var pairingLinkError: String?

    /// Shares a key with the current pairing: the same gateway, the sign-in carries over.
    func isSameGateway(_ p: Pairing) -> Bool {
        pairing != nil && !shared.effectivePins.isDisjoint(with: p.pins)
    }

    /// Pair with `p` (one gateway per iPhone). Another gateway signs out of the old one first.
    func pair(_ p: Pairing) async {
        if !isSameGateway(p), env.tokens.load() != nil || shared.root != nil {
            if let id = env.tokens.load()?.deviceId { try? await client.revoke(deviceId: id) }
            await forgetAccount()
        }
        await env.pair(p)
        pairing = env.pairing
        pendingPairing = nil
        choosingGateway = false
        signedOutReason = nil
        route = await env.reprobe()
        await env.refreshPins()
    }

    /// Password -> device token. Only to a pinned listener (home, or a `direct` public address).
    func signIn(user: String, password: String) async throws {
        guard pairing != nil else { throw SignInFailure.message(PhoneCopy.notPaired) }
        route = await env.reprobe()
        guard env.selector.canPasswordSignIn else { throw SignInFailure.message(PhoneCopy.notAtHome) }
        try await finishSignIn {
            try await self.client.login(user: user.trimmingCharacters(in: .whitespaces),
                                        password: password, deviceName: self.deviceName)
        }
    }

    /// A sign-in code made on the home network (e.g. an app store reviewer's account): any network.
    func signIn(code: String) async throws {
        guard pairing != nil else { throw SignInFailure.message(PhoneCopy.notPaired) }
        route = await env.reprobe()
        try await finishSignIn { try await self.client.codeLogin(code: code, deviceName: self.deviceName) }
    }

    private func finishSignIn(_ login: @escaping () async throws -> LoginResult) async throws {
        let r: LoginResult
        do {
            r = try await login()
        } catch GatewayError.api(let e) {
            appLog.error("sign-in: \(e.status) \(e.error, privacy: .public)")
            throw SignInFailure.message(PhoneCopy.message(forSignIn: e.error))
        } catch {
            appLog.error("sign-in: \(String(describing: error), privacy: .public)")
            throw SignInFailure.message(route == .direct ? PhoneCopy.signInFailed : PhoneCopy.notAtHome)
        }
        if let old = env.tokens.load(), old.deviceId != r.deviceId {
            try? await client.revoke(deviceId: old.deviceId)
        }
        if let root = shared.root { await removeDomain(domain(for: root)) }
        shared.root = nil
        try env.tokens.save(StoredToken(r))
        shared.nasUser = r.nasUser
        shared.deviceId = r.deviceId
        shared.deviceName = deviceName
        let me = try await client.setRoot(share: Phone.allSharesRoot.share, path: Phone.allSharesRoot.path)
        guard let root = me.root else { throw SignInFailure.message(PhoneCopy.signInFailed) }
        shared.root = root
        nasUser = r.nasUser
        signedOutReason = nil
        rootItemId = root.rootItemId
        await ensureDomain()
    }

    func signOut() async {
        if let id = env.tokens.load()?.deviceId { try? await client.revoke(deviceId: id) }
        await forgetAccount()
    }

    private func forgetAccount() async {
        if let root = shared.root { await removeDomain(domain(for: root)) }
        env.tokens.clear()
        shared.clearAccount()
        Phone.lastFolder = nil
        try? FileManager.default.removeItem(at: OpenCache.dir)
        rootItemId = nil
        nasUser = nil
    }

    // MARK: Files location

    private func domain(for root: RootInfo) -> NSFileProviderDomain {
        NSFileProviderDomain(identifier: NSFileProviderDomainIdentifier(rawValue: root.id), displayName: "Loft")
    }

    var domainManager: NSFileProviderManager? {
        shared.root.flatMap { NSFileProviderManager(for: domain(for: $0)) }
    }

    func ensureDomain() async {
        guard let root = shared.root else { return }
        do {
            let want = domain(for: root)
            let have = try await NSFileProviderManager.domains()
            for d in have where d.identifier != want.identifier { await removeDomain(d) }
            if !have.contains(where: { $0.identifier == want.identifier }) {
                try await NSFileProviderManager.add(want)
            }
            shared.domainIdentifier = root.id
        } catch {
            appLog.error("domain: \(String(describing: error), privacy: .public)")
        }
    }

    private func removeDomain(_ d: NSFileProviderDomain) async {
        do { _ = try await NSFileProviderManager.remove(d, mode: .removeAll) } catch {
            appLog.error("domain remove: \(String(describing: error), privacy: .public)")
        }
    }

    // MARK: uploads (camera)

    /// Uploads run while the app is open, plus the extra time iOS gives an app that was
    /// just put away; the photo waits in the app's own storage until it's on the NAS.
    func upload(file: URL, name: String, to folder: FolderRef, deleteAfter: Bool = true) {
        let job = UploadJob(name: name, folder: folder.path)
        uploads.append(job)
        Phone.lastFolder = SavedFolder(id: folder.id, path: folder.path)
        let bg = UIApplication.shared.beginBackgroundTask(withName: "loft-upload")
        Task {
            defer { UIApplication.shared.endBackgroundTask(bg) }
            do {
                let env = self.env
                let item = try await Phone.save(file, as: name, into: folder.id, env: env) { done, total in
                    Task { @MainActor in job.progress = total > 0 ? Double(done) / Double(total) : 0 }
                }
                job.savedAs = item.name
                job.state = .done
                if deleteAfter { try? FileManager.default.removeItem(at: file) }
                NotificationCenter.default.post(name: .loftFolderChanged, object: folder.id)
            } catch {
                appLog.error("upload: \(String(describing: error), privacy: .public)")
                job.state = .failed(PhoneCopy.uploadFailed)
                job.retry = { [self] in
                    uploads.removeAll { $0 === job }
                    upload(file: file, name: name, to: folder, deleteAfter: deleteAfter)
                }
            }
            if case .done = job.state {
                try? await Task.sleep(nanoseconds: 4_000_000_000)
                self.uploads.removeAll { $0 === job }
            }
        }
    }
}

@MainActor
final class UploadJob: ObservableObject, Identifiable {
    enum State: Equatable { case running, done, failed(String) }
    let id = UUID()
    let name: String
    let folder: String
    @Published var progress: Double = 0
    @Published var state: State = .running
    @Published var savedAs: String?
    var retry: (() -> Void)?
    init(name: String, folder: String) { self.name = name; self.folder = folder }
}

extension Notification.Name {
    static let loftFolderChanged = Notification.Name("loftFolderChanged")
}
