import SwiftUI
import LoftCore

struct SignInView: View {
    @EnvironmentObject var model: PhoneModel
    @State private var user = ""
    @State private var password = ""
    @State private var code = ""
    @State private var useCode = false
    @State private var busy = false
    @State private var error: String?
    @FocusState private var focus: Field?
    enum Field { case user, password, code }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    VStack(alignment: .leading, spacing: 8) {
                        Image("AppIconImage").resizable().frame(width: 64, height: 64)
                            .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
                        Text("Your NAS, on your iPhone").font(.title2.bold())
                        Text("Use the username and password you already have on the NAS. Sign in at home the first time, so your password never passes through anyone else's servers. After that, Loft works from anywhere.")
                            .font(.subheadline).foregroundStyle(.secondary)
                    }
                    .padding(.vertical, 6)
                }
                if let reason = model.signedOutReason {
                    Section { Label(reason, systemImage: "exclamationmark.circle").foregroundStyle(.orange) }
                }
                if useCode {
                    Section {
                        TextField("XXXX-XXXX-XXXX-XXXX-XXXX-XXXX", text: $code)
                            .textInputAutocapitalization(.characters).autocorrectionDisabled()
                            .font(.body.monospaced()).focused($focus, equals: .code)
                            .submitLabel(.go).onSubmit(submit)
                    } header: { Text("Sign-in code") } footer: {
                        Text("A sign-in code is made on the home network for a specific account.")
                    }
                } else {
                    Section("NAS account") {
                        TextField("Username", text: $user)
                            .textContentType(.username).textInputAutocapitalization(.never)
                            .autocorrectionDisabled().focused($focus, equals: .user)
                            .submitLabel(.next).onSubmit { focus = .password }
                        SecureField("Password", text: $password)
                            .textContentType(.password).focused($focus, equals: .password)
                            .submitLabel(.go).onSubmit(submit)
                    }
                }
                if let error {
                    Section { Text(error).foregroundStyle(.red) }
                }
                Section {
                    Button(action: submit) {
                        HStack { Spacer(); if busy { ProgressView() } else { Text("Sign in").bold() }; Spacer() }
                    }
                    .disabled(busy || (useCode ? code.count < 24 : user.isEmpty || password.isEmpty))
                    Button(useCode ? String(localized: "Sign in with a password instead") : String(localized: "I have a sign-in code")) {
                        useCode.toggle(); error = nil
                    }
                    .font(.footnote)
                }
                Section {
                    if let p = model.pairing {
                        LabeledContent("Gateway", value: "\(p.name) (\(p.lanHost))")
                    }
                    Button("Pair with another gateway") { model.choosingGateway = true }
                        .font(.footnote)
                }
            }
            .navigationTitle("Loft")
        }
    }

    private func submit() {
        guard !busy else { return }
        busy = true; error = nil
        Task {
            do {
                if useCode { try await model.signIn(code: code) }
                else { try await model.signIn(user: user, password: password) }
                password = ""
            } catch PhoneModel.SignInFailure.message(let m) {
                error = m
            } catch {
                self.error = PhoneCopy.signInFailed
            }
            busy = false
        }
    }
}
