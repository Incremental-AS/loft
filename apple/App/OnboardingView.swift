// First run: 640 pt wide, hero image on step 1 only,
// 64 pt app icon on steps 2–4, primary button bottom right. Words: Copy.swift.
import SwiftUI
import LoftCore

enum OnboardingStep { case welcome, pair, manual, confirmPair, signIn, pickFolder, done }

@MainActor
final class OnboardingFlow: ObservableObject {
    let model: MenuModel
    @Published var step: OnboardingStep
    @Published var user = ""
    @Published var password = ""
    @Published var busy = false
    @Published var message: String?

    // pairing (docs/pairing.md): a link, or address + pin behind "Set up manually"
    @Published var link = ""
    @Published var address = ""
    @Published var pin = ""

    /// After the welcome: pair first if this Mac has no gateway yet.
    func getStarted() { step = model.pairing == nil ? .pair : .signIn }

    func pasteLink() {
        if let s = NSPasteboard.general.string(forType: .string) { link = s.trimmingCharacters(in: .whitespacesAndNewlines) }
        submitLink()
    }

    func submitLink() {
        do {
            model.pendingPairing = try Pairing.parse(link)
            message = nil
            step = .confirmPair
        } catch {
            message = Copy.pairingError(error)
        }
    }

    func submitManual() {
        do {
            model.pendingPairing = try Pairing.manual(address: address, pins: pin)
            message = nil
            step = .confirmPair
        } catch {
            message = Copy.pairingError(error, manual: true)
        }
    }

    func confirmPair() {
        guard let p = model.pendingPairing else { step = .pair; return }
        let keepsAccount = model.isSameGateway(p) && model.isSetUp
        busy = true
        Task {
            defer { busy = false }
            await model.pair(p)
            link = ""; address = ""; pin = ""; message = nil
            if keepsAccount { Windows.shared.close("onboarding") } else { step = .signIn }
        }
    }

    func cancelPair() {
        model.pendingPairing = nil
        message = nil
        step = model.pairing == nil ? .pair : (model.isSetUp ? .done : .signIn)
        if model.isSetUp { Windows.shared.close("onboarding") }
    }

    // folder picker
    @Published var shares: [String] = []
    @Published var share: String?
    @Published var path = ""
    @Published var folders: [String] = []

    init(model: MenuModel, start: OnboardingStep) {
        self.model = model
        self.step = start
        self.user = model.nasUser ?? ""
    }

    func signIn() {
        busy = true
        message = nil
        Task {
            defer { busy = false }
            do {
                try await model.signIn(user: user, password: password)
                password = ""
                // Same account and folder as before (password changed, sign-in expired):
                // carry on in the same Finder location, no folder step.
                if await model.restoreRootIfKnown() {
                    Windows.shared.close("onboarding")
                } else {
                    // Every share this account can open, no folder step.
                    // Settings › Change… still narrows it to one folder.
                    try await model.useFolder(share: RootInfo.allShares, path: "")
                    step = .done
                }
            } catch MenuModel.SignInFailure.message(let m) {
                message = m
            } catch {
                message = Copy.signInFailed
            }
        }
    }

    func loadShares() async {
        busy = true
        defer { busy = false }
        do {
            shares = try await model.client.shares()
            if shares.count == 1 { await open(share: shares[0], path: "") }
        } catch {
            message = Copy.signInFailed
        }
    }

    func open(share s: String?, path p: String) async {
        guard let s else { share = nil; path = ""; folders = []; return }
        busy = true
        defer { busy = false }
        do {
            let r = try await model.client.folders(share: s, path: p)
            share = s; path = r.path; folders = r.folders
            message = nil
        } catch {
            message = Copy.signInFailed
        }
    }

    func up() async {
        guard let s = share else { return }
        if path.isEmpty {
            if shares.count > 1 { await open(share: nil, path: "") }
        } else {
            await open(share: s, path: (path as NSString).deletingLastPathComponent)
        }
    }

    var canGoUp: Bool { share != nil && (!path.isEmpty || shares.count > 1) }
    var location: String { share.map { path.isEmpty ? $0 : "\($0)/\(path)" } ?? "" }

    /// At the share list (no share open) this means all shares.
    func useThisFolder() {
        let s = share ?? RootInfo.allShares
        busy = true
        Task {
            defer { busy = false }
            do {
                try await model.useFolder(share: s, path: path)
                step = .done
            } catch {
                message = Copy.signInFailed
            }
        }
    }
}

struct OnboardingView: View {
    @ObservedObject var flow: OnboardingFlow

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if flow.step == .welcome {
                // Fixed size: the window takes the view's minimum height, and a resizable
                // image's minimum is 0, so with only an aspect ratio the hero vanished.
                Image("welcome-hero").resizable().frame(width: 640, height: 320)
            }
            VStack(alignment: .leading, spacing: 14) {
                if flow.step != .welcome {
                    Image(nsImage: NSApp.applicationIconImage).resizable().frame(width: 64, height: 64)
                }
                content
            }
            .padding(24)
        }
    }

    @ViewBuilder private var content: some View {
        switch flow.step {
        case .welcome:
            Text(Copy.welcomeTitle).font(.largeTitle).fontWeight(.semibold)
            Text(Copy.welcomeBody).fixedSize(horizontal: false, vertical: true)
            footer { Button(Copy.getStarted) { flow.getStarted() }.keyboardShortcut(.defaultAction) }

        case .pair:
            Text(Copy.pairTitle).font(.title).fontWeight(.semibold)
            Text(Copy.pairBody).fixedSize(horizontal: false, vertical: true)
            Form {
                TextField(Copy.pairLinkField, text: $flow.link, prompt: Text(Copy.pairLinkPlaceholder))
                    .onSubmit { flow.submitLink() }
                    .accessibilityIdentifier("pairing-link")
            }
            if let m = flow.message {
                Text(m).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("pairing-error")
            }
            HStack {
                Button(Copy.setUpManually) { flow.message = nil; flow.step = .manual }
                    .buttonStyle(.link)
                    .accessibilityIdentifier("set-up-manually")
                Spacer()
                Button(Copy.paste) { flow.pasteLink() }
                Button(Copy.continueButton) { flow.submitLink() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(flow.link.trimmingCharacters(in: .whitespaces).isEmpty)
            }
            .padding(.top, 8)

        case .manual:
            Text(Copy.manualTitle).font(.title).fontWeight(.semibold)
            Text(Copy.manualBody).fixedSize(horizontal: false, vertical: true)
            Form {
                TextField(Copy.addressField, text: $flow.address, prompt: Text(Copy.addressPlaceholder))
                    .accessibilityIdentifier("manual-address")
                TextField(Copy.pinField, text: $flow.pin, prompt: Text(Copy.pinPlaceholder))
                    .font(.body.monospaced())
                    .accessibilityIdentifier("manual-pin")
            }
            if let m = flow.message {
                Text(m).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("pairing-error")
            }
            HStack {
                Button(Copy.usePairingLink) { flow.message = nil; flow.step = .pair }
                    .buttonStyle(.link)
                    .accessibilityIdentifier("use-pairing-link")
                Spacer()
                Button(Copy.continueButton) { flow.submitManual() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(flow.address.isEmpty || flow.pin.isEmpty)
            }
            .padding(.top, 8)

        case .confirmPair:
            if let p = flow.model.pendingPairing {
                Text(Copy.confirmPairTitle(p.name)).font(.title).fontWeight(.semibold)
                VStack(alignment: .leading, spacing: 6) {
                    Text(Copy.atHome(hostPort(p.lan)))
                    if let pub = p.publicURL {
                        Text(p.publicMode == .direct ? Copy.awayDirect(hostPort(pub)) : Copy.awayTunnel(hostPort(pub)))
                    } else {
                        Text(Copy.homeOnly)
                    }
                }
                .fixedSize(horizontal: false, vertical: true)
                if let cur = flow.model.pairing, !flow.model.isSameGateway(p) {
                    Text(Copy.replacesGateway(cur.name)).foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                }
                footer {
                    if flow.busy { ProgressView().controlSize(.small) }
                    Button(Copy.cancel) { flow.cancelPair() }.keyboardShortcut(.cancelAction)
                    Button(Copy.pair) { flow.confirmPair() }
                        .keyboardShortcut(.defaultAction)
                        .disabled(flow.busy)
                }
            } else {
                Text(Copy.pairTitle).font(.title).fontWeight(.semibold)
                footer { Button(Copy.continueButton) { flow.step = .pair }.keyboardShortcut(.defaultAction) }
            }

        case .signIn:
            Text(Copy.signInTitle).font(.title).fontWeight(.semibold)
            Text(Copy.signInBody).fixedSize(horizontal: false, vertical: true)
            if let n = flow.model.signInNotice {
                Text(n).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
            }
            Form {
                TextField(Copy.usernameField, text: $flow.user, prompt: Text(Copy.usernamePlaceholder))
                    .textContentType(.username)
                    .accessibilityIdentifier("nas-user")
                SecureField(Copy.passwordField, text: $flow.password)
                    .textContentType(.password)
                    .accessibilityIdentifier("nas-password")
            }
            Text(Copy.accountHelp).font(.callout).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if let m = flow.message { Text(m).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true) }
            HStack {
                if let p = flow.model.pairing {
                    Text(Copy.pairedWith(p.name, p.lanHost)).font(.callout).foregroundStyle(.secondary)
                }
                Button(Copy.pairAnother) { flow.message = nil; flow.step = .pair }
                    .buttonStyle(.link).font(.callout)
            }
            footer {
                if flow.busy { ProgressView().controlSize(.small) }
                Button(Copy.signIn) { flow.signIn() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(flow.busy || flow.user.isEmpty || flow.password.isEmpty)
            }
            .task { await flow.model.reprobe() }

        case .pickFolder:
            Text(Copy.pickFolderTitle).font(.title).fontWeight(.semibold)
            Text(Copy.pickFolderBody).fixedSize(horizontal: false, vertical: true)
            HStack {
                Button { Task { await flow.up() } } label: { Image(systemName: "chevron.left") }
                    .disabled(!flow.canGoUp || flow.busy)
                    .accessibilityLabel(Copy.back)
                Text(flow.location).font(.headline)
                Spacer()
                if flow.busy { ProgressView().controlSize(.small) }
            }
            List {
                if flow.share == nil {
                    ForEach(flow.shares, id: \.self) { s in
                        row(s) { await flow.open(share: s, path: "") }
                    }
                } else {
                    ForEach(flow.folders, id: \.self) { f in
                        row(f) {
                            let p = flow.path.isEmpty ? f : "\(flow.path)/\(f)"
                            await flow.open(share: flow.share, path: p)
                        }
                    }
                }
            }
            .frame(height: 220)
            if let m = flow.message { Text(m).foregroundStyle(.red) }
            footer {
                Button(flow.share == nil ? Copy.useAllShares : Copy.useThisFolder) { flow.useThisFolder() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(flow.busy)
            }
            .task { if flow.shares.isEmpty { await flow.loadShares() } }

        case .done:
            Text(Copy.doneTitle).font(.title).fontWeight(.semibold)
            Text(Copy.doneBody).fixedSize(horizontal: false, vertical: true)
            footer {
                Button(Copy.openLoftInFinder) {
                    flow.model.openInFinder()
                    Windows.shared.close("onboarding")
                }
                .keyboardShortcut(.defaultAction)
            }
        }
    }

    private func hostPort(_ u: URL) -> String {
        guard let h = u.host else { return u.absoluteString }
        return u.port.map { "\(h):\($0)" } ?? h
    }

    private func row(_ name: String, _ open: @escaping () async -> Void) -> some View {
        Button { Task { await open() } } label: {
            Label(name, systemImage: "folder").frame(maxWidth: .infinity, alignment: .leading)
        }
        .buttonStyle(.plain)
        .disabled(flow.busy)
    }

    private func footer<C: View>(@ViewBuilder _ c: () -> C) -> some View {
        HStack { Spacer(); c() }.padding(.top, 8)
    }
}
