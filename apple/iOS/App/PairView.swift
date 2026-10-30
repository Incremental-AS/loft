// Pairing (docs/pairing.md): the first screen of a new install. Scan the QR
// code `loft-gateway pair` shows, open its link (universal link), paste it, or — behind
// "Set up manually" — type the address and a key pin. Every way ends in ConfirmPairView,
// which shows what the code points at before anything changes.
import AVFoundation
import LoftCore
import SwiftUI

struct PairView: View {
    @EnvironmentObject var model: PhoneModel
    @State private var link = ""
    @State private var error: String?
    @State private var scanning = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    VStack(alignment: .leading, spacing: 8) {
                        Image("AppIconImage").resizable().frame(width: 64, height: 64)
                            .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
                        Text("Pair with your gateway").font(.title2.bold())
                        Text("On the gateway, run loft-gateway pair. Scan the QR code it shows, or open its link on this iPhone. The code carries the gateway's address and its keys, so Loft talks to your gateway from the very first connection.")
                            .font(.subheadline).foregroundStyle(.secondary)
                    }
                    .padding(.vertical, 6)
                }
                Section {
                    Button { scanning = true } label: { Label("Scan QR code", systemImage: "qrcode.viewfinder") }
                }
                Section {
                    TextField("https://loft.incremental.no/pair#…", text: $link)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                        .keyboardType(.URL).submitLabel(.go).onSubmit(submit)
                        .accessibilityIdentifier("pairing-link")
                    PasteButton(payloadType: String.self) { s in
                        Task { @MainActor in
                            link = s.first?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
                            submit()
                        }
                    }
                } header: { Text("Pairing link") } footer: {
                    Text("Everything after the # stays on this iPhone.")
                }
                if let error {
                    Section { Text(error).foregroundStyle(.red) }
                }
                Section {
                    Button(action: submit) { HStack { Spacer(); Text("Continue").bold(); Spacer() } }
                        .disabled(link.trimmingCharacters(in: .whitespaces).isEmpty)
                    NavigationLink("Set up manually") { ManualPairView() }
                        .font(.footnote)
                }
            }
            .navigationTitle("Loft")
            .toolbar {
                if model.choosingGateway {
                    ToolbarItem(placement: .cancellationAction) {
                        Button("Cancel") { model.choosingGateway = false }
                    }
                }
            }
            .sheet(isPresented: $scanning) {
                QRScanSheet { text in
                    scanning = false
                    link = text
                    submit()
                }
            }
        }
    }

    private func submit() {
        do {
            try model.offerPairing(link)
            error = nil
        } catch PhoneModel.SignInFailure.message(let m) {
            error = m
        } catch {
            self.error = PhoneCopy.pairingNotALink
        }
    }
}

struct ManualPairView: View {
    @EnvironmentObject var model: PhoneModel
    @State private var address = ""
    @State private var pin = ""
    @State private var error: String?

    var body: some View {
        Form {
            Section {
                Text("Type the gateway's address and one of its key fingerprints. loft-gateway pair shows both. The fingerprint is how Loft checks it's really your gateway, so there's no setup without it.")
                    .font(.subheadline).foregroundStyle(.secondary)
            }
            Section("Gateway") {
                TextField("Address, like nas-gateway.local:8443", text: $address)
                    .textInputAutocapitalization(.never).autocorrectionDisabled().keyboardType(.URL)
                TextField("Key fingerprint", text: $pin)
                    .textInputAutocapitalization(.never).autocorrectionDisabled()
                    .font(.body.monospaced())
            }
            if let error {
                Section { Text(error).foregroundStyle(.red) }
            }
            Section {
                Button(action: submit) { HStack { Spacer(); Text("Continue").bold(); Spacer() } }
                    .disabled(address.isEmpty || pin.isEmpty)
            }
        }
        .navigationTitle("Set up manually")
    }

    private func submit() {
        do {
            try model.offerManualPairing(address: address, pin: pin)
            error = nil
        } catch PhoneModel.SignInFailure.message(let m) {
            error = m
        } catch {
            self.error = PhoneCopy.manualBadAddress
        }
    }
}

/// What the code points at, and a yes or no. Pairing with another gateway signs out of
/// the current one.
struct ConfirmPairView: View {
    @EnvironmentObject var model: PhoneModel
    @State private var busy = false

    var body: some View {
        NavigationStack {
            Form {
                if let p = model.pendingPairing {
                    Section {
                        Text("Pair with “\(p.name)”?").font(.title3.bold())
                    }
                    Section {
                        LabeledContent("At home", value: hostPort(p.lan))
                        if let pub = p.publicURL {
                            LabeledContent("Away from home", value: hostPort(pub))
                        }
                    } footer: {
                        if p.publicURL == nil {
                            Text("No outside address: Loft works on the home network only.")
                        } else if p.publicMode == .direct {
                            Text("Away from home, Loft connects straight to the gateway and checks its keys every time.")
                        } else {
                            Text("Away from home, Loft goes through a tunnel. Sign in at home first.")
                        }
                    }
                    if let cur = model.pairing, !model.isSameGateway(p) {
                        Section {
                            Label("This iPhone is paired with \(cur.name). If you pair with another gateway, you'll be signed out of \(cur.name).",
                                  systemImage: "exclamationmark.triangle")
                                .foregroundStyle(.orange)
                        }
                    }
                    Section {
                        Button {
                            busy = true
                            Task { await model.pair(p); busy = false }
                        } label: {
                            HStack { Spacer(); if busy { ProgressView() } else { Text("Pair").bold() }; Spacer() }
                        }
                        .disabled(busy)
                        .accessibilityIdentifier("confirm-pair")
                    }
                }
            }
            .navigationTitle("Pair")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { model.pendingPairing = nil }.disabled(busy)
                }
            }
        }
        .interactiveDismissDisabled(busy)
    }

    private func hostPort(_ u: URL) -> String {
        guard let h = u.host else { return u.absoluteString }
        return u.port.map { "\(h):\($0)" } ?? h
    }
}

// MARK: QR scanner

struct QRScanSheet: View {
    let found: (String) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var problem: String?

    var body: some View {
        NavigationStack {
            ZStack {
                if let problem {
                    ContentUnavailableView("Can't scan", systemImage: "camera", description: Text(problem))
                } else {
                    QRScannerView(found: found, failed: { problem = $0 }).ignoresSafeArea()
                }
            }
            .navigationTitle("Scan QR code")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } } }
        }
        .task { await checkCamera() }
    }

    private func checkCamera() async {
        guard AVCaptureDevice.default(for: .video) != nil else { problem = PhoneCopy.noCamera; return }
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized: break
        case .notDetermined:
            if !(await AVCaptureDevice.requestAccess(for: .video)) { problem = PhoneCopy.cameraDenied }
        default: problem = PhoneCopy.cameraDenied
        }
    }
}

/// AVFoundation QR reader: the first code that is a Loft pairing link wins. Other QR codes
/// are ignored, so pointing at the wrong one does nothing.
struct QRScannerView: UIViewControllerRepresentable {
    let found: (String) -> Void
    let failed: (String) -> Void

    func makeUIViewController(context: Context) -> ScannerController {
        let c = ScannerController()
        c.found = found
        c.failed = failed
        return c
    }
    func updateUIViewController(_ c: ScannerController, context: Context) {}

    final class ScannerController: UIViewController, AVCaptureMetadataOutputObjectsDelegate {
        var found: ((String) -> Void)?
        var failed: ((String) -> Void)?
        private let session = AVCaptureSession()
        private var preview: AVCaptureVideoPreviewLayer?
        private var done = false

        override func viewDidLoad() {
            super.viewDidLoad()
            view.backgroundColor = .black
            guard let device = AVCaptureDevice.default(for: .video),
                  let input = try? AVCaptureDeviceInput(device: device), session.canAddInput(input) else {
                failed?(PhoneCopy.noCamera)
                return
            }
            session.addInput(input)
            let out = AVCaptureMetadataOutput()
            guard session.canAddOutput(out) else { failed?(PhoneCopy.noCamera); return }
            session.addOutput(out)
            out.setMetadataObjectsDelegate(self, queue: .main)
            out.metadataObjectTypes = [.qr]
            let p = AVCaptureVideoPreviewLayer(session: session)
            p.videoGravity = .resizeAspectFill
            view.layer.addSublayer(p)
            preview = p
        }

        override func viewDidLayoutSubviews() {
            super.viewDidLayoutSubviews()
            preview?.frame = view.bounds
        }

        override func viewWillAppear(_ animated: Bool) {
            super.viewWillAppear(animated)
            let s = session
            DispatchQueue.global(qos: .userInitiated).async { if !s.isRunning { s.startRunning() } }
        }

        override func viewWillDisappear(_ animated: Bool) {
            super.viewWillDisappear(animated)
            let s = session
            DispatchQueue.global(qos: .userInitiated).async { if s.isRunning { s.stopRunning() } }
        }

        func metadataOutput(_ output: AVCaptureMetadataOutput, didOutput objects: [AVMetadataObject],
                            from connection: AVCaptureConnection) {
            guard !done else { return }
            for case let o as AVMetadataMachineReadableCodeObject in objects {
                // A pairing link, even a faulty one (the form then says what's wrong with it).
                guard let s = o.stringValue,
                      URL(string: s).map(Pairing.isPairingURL) == true || (try? Pairing.parse(s)) != nil
                else { continue }
                done = true
                UINotificationFeedbackGenerator().notificationOccurred(.success)
                found?(s)
                return
            }
        }
    }
}
