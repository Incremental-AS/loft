// Choose a NAS folder (share extension; could serve "Move" later). Starts with the last
// folder something was saved to, if there is one.
import SwiftUI
import LoftCore

struct FolderPicker: View {
    let env: LoftEnvironment
    let title: String
    let action: String
    let chosen: (SavedFolder) -> Void
    @State private var path: [PickerLevel] = []

    var body: some View {
        NavigationStack(path: $path) {
            List {
                if let last = Phone.lastFolder {
                    Section("Last used") {
                        Button { chosen(last) } label: { Label(last.path, systemImage: "clock.arrow.circlepath") }
                    }
                }
                Section("Shares") {
                    PickerLevelList(env: env, level: rootLevel, isShareList: true)
                }
            }
            .navigationTitle(title).navigationBarTitleDisplayMode(.inline)
            .navigationDestination(for: PickerLevel.self) { level in
                List { PickerLevelList(env: env, level: level, isShareList: false) }
                    .navigationTitle(level.title)
                    .toolbar {
                        ToolbarItem(placement: .confirmationAction) {
                            Button(action) { chosen(SavedFolder(id: level.id, path: level.path)) }.bold()
                        }
                    }
            }
        }
    }

    private var rootLevel: PickerLevel {
        PickerLevel(id: env.shared.root?.rootItemId ?? "", title: "Loft", path: "")
    }
}

struct PickerLevel: Hashable { let id: String; let title: String; let path: String }

struct PickerLevelList: View {
    let env: LoftEnvironment
    let level: PickerLevel
    let isShareList: Bool
    @State private var folders: [Item]?
    @State private var failed = false

    var body: some View {
        Group {
            if let folders {
                if folders.isEmpty {
                    Text(isShareList ? String(localized: "No shares.") : String(localized: "No folders inside.")).foregroundStyle(.secondary)
                }
                ForEach(folders, id: \.id) { f in
                    NavigationLink(value: PickerLevel(id: f.id, title: f.name,
                                                      path: level.path.isEmpty ? f.name : "\(level.path)/\(f.name)")) {
                        Label(f.name, systemImage: isShareList ? "externaldrive.fill" : "folder.fill")
                    }
                }
            } else if failed {
                Text("Can't reach home.").foregroundStyle(.secondary)
            } else {
                ProgressView()
            }
        }
        .task {
            do {
                var all: [Item] = [], cursor: String?
                repeat {
                    let page = try await env.client.children(level.id, cursor: cursor)
                    all += page.items.filter(\.isDirectory); cursor = page.nextCursor
                } while cursor != nil
                folders = all.sorted { $0.name.localizedStandardCompare($1.name) == .orderedAscending }
            } catch { failed = true }
        }
    }
}
