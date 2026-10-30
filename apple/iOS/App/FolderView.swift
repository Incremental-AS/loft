import QuickLook
import SwiftUI
import UniformTypeIdentifiers
import LoftCore

struct FolderView: View {
    @EnvironmentObject var model: PhoneModel
    let folder: FolderRef
    @State private var items: [Item] = []
    @State private var loaded = false
    @State private var error: String?
    @State private var showCamera = false
    @State private var newFolder = false
    @State private var newFolderName = ""
    @State private var confirmDelete: Item?
    @State private var showAccount = false

    var body: some View {
        List {
            if folder.isShareList {
                UploadsSection()
            }
            ForEach(items, id: \.id) { item in
                if item.isDirectory {
                    NavigationLink(value: child(item)) { ItemRow(item: item) }
                        .contextMenu { if !folder.isShareList { deleteButton(item) } }
                } else {
                    NavigationLink(value: item) { ItemRow(item: item) }
                        .contextMenu { deleteButton(item) }
                }
            }
            if loaded && items.isEmpty && error == nil {
                Text(folder.isShareList ? String(localized: "This account can't open any shares.") : String(localized: "This folder is empty."))
                    .foregroundStyle(.secondary)
            }
            if let error {
                Label(error, systemImage: "wifi.exclamationmark").foregroundStyle(.secondary)
            }
        }
        .listStyle(.plain)
        .overlay { if !loaded { ProgressView() } }
        .navigationTitle(folder.title)
        .navigationDestination(for: FolderRef.self) { FolderView(folder: $0) }
        .navigationDestination(for: Item.self) { FileView(item: $0) }
        .refreshable { await load() }
        .task { if !loaded { await load() } }
        .onReceive(NotificationCenter.default.publisher(for: .loftFolderChanged)) { n in
            if (n.object as? String) == folder.id { Task { await load() } }
        }
        .toolbar {
            if folder.isShareList {
                ToolbarItem(placement: .topBarLeading) {
                    Button { showAccount = true } label: { Image(systemName: "person.crop.circle") }
                        .accessibilityLabel("Account")
                }
            } else {
                ToolbarItem(placement: .topBarTrailing) {
                    Menu {
                        Button { newFolderName = ""; newFolder = true } label: {
                            Label("New folder", systemImage: "folder.badge.plus")
                        }
                    } label: { Image(systemName: "ellipsis.circle") }
                }
                ToolbarItem(placement: .bottomBar) {
                    Button { showCamera = true } label: {
                        Label("Take photo", systemImage: "camera").labelStyle(.titleAndIcon)
                    }
                    .disabled(!CameraView.isAvailable)
                }
            }
        }
        .toolbar(folder.isShareList ? .hidden : .visible, for: .bottomBar)
        .fullScreenCover(isPresented: $showCamera) {
            CameraView(folder: folder).ignoresSafeArea()
        }
        .sheet(isPresented: $showAccount) { AccountView() }
        .alert("New folder", isPresented: $newFolder) {
            TextField("Name", text: $newFolderName)
            Button("Cancel", role: .cancel) {}
            Button("Create") { Task { await makeFolder() } }
        }
        .confirmationDialog(confirmDelete.map { String(localized: "Move “\($0.name)” to the Loft trash?") } ?? "",
                            isPresented: Binding(get: { confirmDelete != nil }, set: { if !$0 { confirmDelete = nil } }),
                            titleVisibility: .visible) {
            Button("Move to trash", role: .destructive) { if let it = confirmDelete { Task { await delete(it) } } }
        } message: { Text("It stays in the trash on the NAS for 30 days.") }
    }

    private func child(_ it: Item) -> FolderRef {
        FolderRef(id: it.id, title: it.name, path: folder.path.isEmpty ? it.name : "\(folder.path)/\(it.name)")
    }

    private func deleteButton(_ it: Item) -> some View {
        Button(role: .destructive) { confirmDelete = it } label: { Label("Move to trash", systemImage: "trash") }
    }

    private func load() async {
        do {
            var all: [Item] = [], cursor: String?
            repeat {
                let page = try await model.client.children(folder.id, cursor: cursor)
                all += page.items; cursor = page.nextCursor
            } while cursor != nil
            items = all.sorted { a, b in
                a.isDirectory != b.isDirectory ? a.isDirectory
                    : a.name.localizedStandardCompare(b.name) == .orderedAscending
            }
            error = nil
        } catch {
            self.error = String(localized: "Can't reach home. Pull down to try again.")
        }
        loaded = true
    }

    private func makeFolder() async {
        let name = Phone.safeName(newFolderName)
        do { _ = try await model.client.mkdir(parentId: folder.id, name: name); await load() }
        catch { self.error = String(localized: "Couldn't create the folder.") }
    }

    private func delete(_ it: Item) async {
        do { _ = try await model.client.delete(it.id, ifMatch: it.contentEtag); await load() }
        catch { self.error = String(localized: "Couldn't move it to the trash.") }
        confirmDelete = nil
    }
}

extension Item: @retroactive Hashable {
    public func hash(into h: inout Hasher) { h.combine(id); h.combine(contentEtag) }
}

struct ItemRow: View {
    let item: Item
    var body: some View {
        HStack(spacing: 12) {
            Thumb(item: item).frame(width: 44, height: 44)
            VStack(alignment: .leading, spacing: 2) {
                Text(item.name).lineLimit(2)
                if !item.isDirectory {
                    Text("\(ByteCountFormatter.string(fromByteCount: item.size, countStyle: .file)) · \(item.modified.formatted(date: .abbreviated, time: .shortened))")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
        }
    }
}

/// Gateway thumbnail for photos, PDFs and videos; the type's symbol otherwise.
struct Thumb: View {
    @EnvironmentObject var model: PhoneModel
    let item: Item
    @State private var image: UIImage?

    var body: some View {
        Group {
            if let image {
                // Fill the row's square and crop: a bare scaledToFill spills past the
                // caller's frame, so wide photos covered the start of the file name.
                Color.clear
                    .overlay(Image(uiImage: image).resizable().scaledToFill())
                    .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
            } else {
                Image(systemName: symbol).font(.title2)
                    .foregroundStyle(item.isDirectory ? Color.accentColor : .secondary)
            }
        }
        .task(id: item.contentEtag) {
            guard !item.isDirectory, Thumb.hasThumb(item.name) else { return }
            if let hit = ThumbCache.shared.object(forKey: key) { image = hit; return }
            if let d = try? await model.client.thumbnail(item.id, px: 132), let img = UIImage(data: d) {
                ThumbCache.shared.setObject(img, forKey: key)
                image = img
            }
        }
    }

    private var key: NSString { "\(item.id)-\(item.contentEtag)" as NSString }
    private var type: UTType? { UTType(filenameExtension: (item.name as NSString).pathExtension) }

    static func hasThumb(_ name: String) -> Bool {
        guard let t = UTType(filenameExtension: (name as NSString).pathExtension) else { return false }
        return t.conforms(to: .image) || t.conforms(to: .movie) || t.conforms(to: .pdf)
    }

    private var symbol: String {
        if item.isDirectory { return "folder.fill" }
        guard let t = type else { return "doc" }
        if t.conforms(to: .image) { return "photo" }
        if t.conforms(to: .movie) { return "film" }
        if t.conforms(to: .audio) { return "music.note" }
        if t.conforms(to: .pdf) { return "doc.richtext" }
        if t.conforms(to: .archive) { return "doc.zipper" }
        if t.conforms(to: .text) { return "doc.text" }
        return "doc"
    }
}

enum ThumbCache { static let shared = NSCache<NSString, UIImage>() }

struct UploadsSection: View {
    @EnvironmentObject var model: PhoneModel
    var body: some View {
        if !model.uploads.isEmpty {
            Section("Saving to Loft") {
                ForEach(model.uploads) { UploadRow(job: $0) }
            }
        }
    }
}

struct UploadRow: View {
    @ObservedObject var job: UploadJob
    var body: some View {
        Button { job.retry?() } label: {
            VStack(alignment: .leading, spacing: 4) {
                Text(job.savedAs ?? job.name).lineLimit(1)
                switch job.state {
                case .running: ProgressView(value: job.progress)
                case .done: Label("Saved in \(job.folder)", systemImage: "checkmark.circle.fill")
                        .font(.caption).foregroundStyle(.green)
                case .failed(let m): Text(m).font(.caption).foregroundStyle(.red)
                }
            }
        }
        .disabled(job.retry == nil)
        .foregroundStyle(.primary)
    }
}

struct AccountView: View {
    @EnvironmentObject var model: PhoneModel
    @Environment(\.dismiss) private var dismiss
    @State private var confirm = false
    var body: some View {
        NavigationStack {
            Form {
                Section("Signed in as") { Text(model.nasUser ?? "-") }
                Section {
                    if let p = model.pairing {
                        LabeledContent("Gateway", value: "\(p.name) (\(p.lanHost))")
                    }
                    LabeledContent("Connection", value: model.route == .direct
                                   ? String(localized: "At home") : String(localized: "Away from home"))
                } footer: {
                    Text("Loft is also in the Files app, and in the share sheet of other apps.")
                }
                Section {
                    Button("Pair with another gateway") { dismiss(); model.choosingGateway = true }
                    Button("Sign out", role: .destructive) { confirm = true }
                } footer: {
                    Text("Pairing with another gateway signs you out of this one.")
                }
            }
            .navigationTitle("Account")
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
            .confirmationDialog("Sign out of Loft on this iPhone?", isPresented: $confirm, titleVisibility: .visible) {
                Button("Sign out", role: .destructive) { Task { await model.signOut(); dismiss() } }
            }
        }
    }
}
