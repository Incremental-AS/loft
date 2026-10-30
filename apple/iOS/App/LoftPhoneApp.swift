// Loft for iPhone: browse the NAS, open and stream files, take
// photos straight into a folder. "Save to Loft" from other apps is the share extension
// (Share/) and Files' own "Save to Files › Loft" (the File Provider extension, shared
// with the Mac in FileProvider/).
import SwiftUI
import LoftCore

@main
struct LoftPhoneApp: App {
    @StateObject private var model = PhoneModel()
    @Environment(\.scenePhase) private var phase

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(model)
                .task { await model.start() }
                .onChange(of: phase) { _, p in if p == .active { Task { await model.becameActive() } } }
                // Universal link https://loft.incremental.no/pair#… (associated domains).
                .onOpenURL { u in if Pairing.isPairingURL(u) { model.openPairingLink(u) } }
                .onContinueUserActivity(NSUserActivityTypeBrowsingWeb) { a in
                    if let u = a.webpageURL, Pairing.isPairingURL(u) { model.openPairingLink(u) }
                }
        }
    }
}

struct RootView: View {
    @EnvironmentObject var model: PhoneModel

    var body: some View {
        Group {
            if !model.started {
                ProgressView()
            } else if model.pairing == nil || model.choosingGateway {
                PairView()
            } else if let root = model.rootItemId {
                NavigationStack {
                    FolderView(folder: FolderRef(id: root, title: "Loft", path: "", isShareList: true))
                }
            } else {
                SignInView()
            }
        }
        .tint(Color("AccentColor"))
        .sheet(isPresented: Binding(get: { model.pendingPairing != nil },
                                    set: { if !$0 { model.pendingPairing = nil } })) {
            ConfirmPairView().environmentObject(model)
        }
        .alert(PhoneCopy.pairingTitle, isPresented: Binding(get: { model.pairingLinkError != nil },
                                                            set: { if !$0 { model.pairingLinkError = nil } })) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(model.pairingLinkError ?? "")
        }
    }
}

struct FolderRef: Hashable {
    let id: String
    let title: String
    let path: String           // "Share/sub/folder", for the last-used folder label
    var isShareList = false    // the top level lists shares; nothing can be created there
}
