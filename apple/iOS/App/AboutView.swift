// About Loft: what the app is, that it needs a gateway at home, and the version. Reached
// from Account, the pairing screen and the sign-in screen, so it is there before pairing
// too. The words follow the website (home page, How it works); the link labels are the
// website's own menu words.
import SwiftUI

/// As a sheet (pairing and sign-in screens).
struct AboutView: View {
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        NavigationStack {
            AboutContent()
                .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
        }
    }
}

/// The page itself; Account pushes it inside its own navigation.
struct AboutContent: View {
    var body: some View {
        Form {
            Section {
                VStack(alignment: .leading, spacing: 8) {
                    Image("AppIconImage").resizable().frame(width: 64, height: 64)
                        .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
                        .accessibilityHidden(true)
                    Text(verbatim: "Loft").font(.title2.bold())
                    Text(verbatim: PhoneCopy.appVersion).font(.subheadline).foregroundStyle(.secondary)
                        .accessibilityIdentifier("about-version")
                }
                .padding(.vertical, 6)
            }
            Section(PhoneCopy.aboutHowTitle) {
                Text(PhoneCopy.aboutHowFiles)
                Text(PhoneCopy.aboutHowParts)
                Text(PhoneCopy.aboutHowSignIn)
                Link(destination: PhoneCopy.websiteURL("how-it-works")) {
                    Label(PhoneCopy.linkHowItWorks, systemImage: "safari")
                }
            }
            Section(PhoneCopy.aboutGatewayTitle) {
                Text(PhoneCopy.aboutGateway)
                Link(destination: PhoneCopy.gatewayGuideURL) {
                    Label(PhoneCopy.setUpGateway, systemImage: "safari")
                }
            }
            Section {
                Text("Loft is also in the Files app, and in the share sheet of other apps.")
            }
            Section {
                LabeledContent("Version", value: PhoneCopy.appVersion)
                Link(destination: PhoneCopy.websiteURL("privacy")) {
                    Label(PhoneCopy.linkPrivacy, systemImage: "hand.raised")
                }
                Link(destination: PhoneCopy.websiteURL("source")) {
                    Label(PhoneCopy.linkSource, systemImage: "chevron.left.forwardslash.chevron.right")
                }
            } footer: {
                Text(PhoneCopy.aboutFree)
            }
        }
        .navigationTitle(PhoneCopy.aboutTitle)
        .navigationBarTitleDisplayMode(.inline)
    }
}

/// The ⓘ button on the screens before sign-in.
struct AboutButton: View {
    @State private var shown = false
    var body: some View {
        Button { shown = true } label: { Image(systemName: "info.circle") }
            .accessibilityLabel(PhoneCopy.aboutTitle)
            .accessibilityIdentifier("about-button")
            .sheet(isPresented: $shown) { AboutView() }
    }
}
