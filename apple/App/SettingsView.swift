// Settings: Account · Folder · Storage · General. Native macOS look.
import SwiftUI
import LoftCore

struct SettingsView: View {
    @ObservedObject var model: MenuModel
    @State private var evictAfter = SharedState().evictAfterMinutes

    var body: some View {
        TabView(selection: $model.settingsTab) {
            account.tabItem { Text(Copy.tabAccount) }.tag(SettingsTab.account)
            folder.tabItem { Text(Copy.tabFolder) }.tag(SettingsTab.folder)
            storage.tabItem { Text(Copy.tabStorage) }.tag(SettingsTab.storage)
            general.tabItem { Text(Copy.tabGeneral) }.tag(SettingsTab.general)
        }
        .padding(20)
    }

    private var account: some View {
        Form {
            if let p = model.pairing {
                Text(Copy.pairedWith(p.name, p.lanHost))
            } else {
                Text(Copy.notPaired)
            }
            Button(model.pairing == nil ? Copy.pairEllipsis : Copy.pairAnother) {
                model.showOnboarding(step: .pair)
            }
            if let u = model.nasUser {
                Text(Copy.signedInAs(u))
                if let d = model.deviceName { Text(Copy.thisMac(d)) }
                if !model.otherDevices.isEmpty {
                    Section(Copy.otherDevices) {
                        ForEach(model.otherDevices) { Text($0.deviceName) }
                    }
                }
                Button(Copy.signOut) { Task { await model.signOut() } }
            } else {
                Button(Copy.signInAgain) { model.showOnboarding() }
            }
        }
    }

    private var folder: some View {
        Form {
            if let p = model.rootPath { Text(Copy.loftFolder(p)) }
            Button(Copy.change) { model.showOnboarding(step: .pickFolder) }
                .disabled(model.nasUser == nil)
        }
    }

    private var storage: some View {
        Form {
            Text(Copy.storageHeading).font(.headline)
            Picker(Copy.freeUpSpaceAfter, selection: $evictAfter) {
                ForEach([5, 10, 30, 60, 240], id: \.self) { Text(Copy.minutes($0)).tag($0) }
            }
            .onChange(of: evictAfter) { _, v in model.shared.evictAfterMinutes = v }
            Text(Copy.storageHelp).font(.callout).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            Text(Copy.officeAutoSaveNote).font(.callout).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var general: some View {
        Form {
            Toggle(Copy.openAtLogin, isOn: Binding(get: { model.openAtLogin },
                                                   set: { model.setOpenAtLogin($0) }))
            if model.updater != nil {
                Toggle(Copy.checkForUpdates, isOn: Binding(get: { model.updateCheckOn },
                                                           set: { model.setUpdateCheck($0) }))
                Text(Copy.updateCheckHelp).font(.callout).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}
