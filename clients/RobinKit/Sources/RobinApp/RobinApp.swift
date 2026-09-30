import RobinKit
import SwiftUI

#if os(macOS)
import AppKit
#endif

@MainActor
final class ShellModel: ObservableObject {
    @Published private(set) var phase: ShellPhase = .signedOut
    @Published var instance = "http://127.0.0.1:8787"
    @Published var accountID = ""
    @Published var password = ""
    @Published var conversationID = "home"
    @Published var draft = ""
    @Published var imapHost = ""
    @Published var smtpHost = ""
    @Published var mailUser = ""
    @Published var mailPassword = ""
    @Published var calendarURL = ""
    @Published var calendarUser = ""
    @Published var calendarPassword = ""
    @Published var scheduleOn = false
    @Published var givenName = ""
    @Published var familyName = ""
    @Published var profileEmail = ""
    @Published var profilePhone = ""
    @Published var profileAddress = ""
    @Published var profileCity = ""
    @Published var profilePostalCode = ""
    @Published var profileCountry = ""
    @Published var vaultPassphrase = ""
    @Published var vaultExport = ""
    @Published var failure: String?
    @Published private(set) var waiting = false
    @Published private(set) var serverTiming = ""

    private let shell: Shell

    init(shell: Shell = Shell()) {
        self.shell = shell
    }

    func signIn() async {
        failure = nil
        guard let url = URL(string: instance), !accountID.isEmpty, !password.isEmpty else {
            failure = "Instance, account, and password are required."
            return
        }
        guard !waiting else { return }
        let secret = password
        password = ""
        waiting = true
        defer { waiting = false }
        do {
            try await shell.signIn(instance: url, accountID: accountID, password: secret)
            scheduleOn = await shell.scheduleEnabled
            await loadProfile()
            phase = await shell.phase
        } catch let error as RobinFailure {
            failure = error.message
        } catch {
            failure = _reach(error)
        }
    }

    func send() async {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !waiting else { return }
        draft = ""
        await perform {
            try await self.shell.send(conversationID: self.conversationID, text: text)
        }
    }

    func confirm() async {
        await perform {
            try await self.shell.confirm(conversationID: self.conversationID)
        }
    }

    func liveViewURL(path: String) -> URL? {
        let base = instance.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !base.isEmpty, !path.isEmpty else { return nil }
        if path.hasPrefix("http://") || path.hasPrefix("https://") {
            return URL(string: path)
        }
        let root = base.hasSuffix("/") ? String(base.dropLast()) : base
        let suffix = path.hasPrefix("/") ? path : "/" + path
        return URL(string: root + suffix)
    }

    func connectMail() async {
        let secret = mailPassword
        mailPassword = ""
        await perform {
            try await self.shell.connectMailbox(
                imapHost: self.imapHost,
                smtpHost: self.smtpHost,
                user: self.mailUser,
                password: secret
            )
        }
    }

    func connectCalendar() async {
        let secret = calendarPassword
        calendarPassword = ""
        await perform {
            try await self.shell.connectCalendar(url: self.calendarURL, user: self.calendarUser, password: secret)
        }
    }

    func saveSchedule() async {
        await perform {
            try await self.shell.setSchedule(enabled: self.scheduleOn)
        }
    }

    func saveProfile() async {
        await perform {
            let saved = try await self.shell.saveProfile([
                "given_name": self.givenName,
                "family_name": self.familyName,
                "email": self.profileEmail,
                "phone": self.profilePhone,
                "address": self.profileAddress,
                "city": self.profileCity,
                "postal_code": self.profilePostalCode,
                "country": self.profileCountry,
            ])
            self.applyProfile(saved)
        }
    }

    func exportVault() async {
        let secret = vaultPassphrase
        vaultPassphrase = ""
        failure = nil
        guard !waiting else { return }
        waiting = true
        defer { waiting = false }
        do {
            vaultExport = try await shell.exportVault(passphrase: secret)
            phase = await shell.phase
        } catch let error as RobinFailure {
            failure = error.message
        } catch {
            failure = _reach(error)
        }
    }

    func importVault() async {
        let secret = vaultPassphrase
        let bundle = vaultExport
        vaultPassphrase = ""
        await perform {
            try await self.shell.importVault(passphrase: secret, export: bundle)
        }
    }

    func leave() async {
        await shell.leave()
        draft = ""
        mailPassword = ""
        calendarPassword = ""
        vaultPassphrase = ""
        vaultExport = ""
        scheduleOn = false
        clearProfile()
        serverTiming = ""
        failure = nil
        phase = .signedOut
    }

    private func loadProfile() async {
        do {
            applyProfile(try await shell.profile())
        } catch {
            clearProfile()
        }
    }

    private func applyProfile(_ fields: [String: String]) {
        givenName = fields["given_name"] ?? ""
        familyName = fields["family_name"] ?? ""
        profileEmail = fields["email"] ?? ""
        profilePhone = fields["phone"] ?? ""
        profileAddress = fields["address"] ?? ""
        profileCity = fields["city"] ?? ""
        profilePostalCode = fields["postal_code"] ?? ""
        profileCountry = fields["country"] ?? ""
    }

    private func clearProfile() {
        givenName = ""
        familyName = ""
        profileEmail = ""
        profilePhone = ""
        profileAddress = ""
        profileCity = ""
        profilePostalCode = ""
        profileCountry = ""
    }

    private func perform(_ work: () async throws -> Void) async {
        guard !waiting else { return }
        failure = nil
        waiting = true
        defer { waiting = false }
        do {
            try await work()
            phase = await shell.phase
            serverTiming = await shell.serverTiming
        } catch let error as RobinFailure {
            failure = error.message
        } catch {
            failure = _reach(error)
        }
    }
}

private func _reach(_ error: Error) -> String {
    if let urlError = error as? URLError, urlError.code == .timedOut {
        return "Robin took too long to answer."
    }
    let text = String(describing: error).lowercased()
    if text.contains("timed out") || text.contains("timeout") || text.contains("-1001") {
        return "Robin took too long to answer."
    }
    return "Could not reach Robin."
}

struct RobinRootView: View {
    @StateObject private var model = ShellModel()

    var body: some View {
        NavigationStack {
            switch model.phase {
            case .signedOut:
                SignInForm(model: model)
            case .ready, .confirm, .handoff:
                ConversationForm(model: model)
            }
        }
    }
}

private struct SignInForm: View {
    @ObservedObject var model: ShellModel

    var body: some View {
        RobinFields {
            TextField("Instance", text: $model.instance)
                .robinField()
            TextField("Account", text: $model.accountID)
                .robinField()
            SecureField("Password", text: $model.password)
                .robinField()
            if model.waiting {
                Text("Waiting for Robin…")
            }
            if let failure = model.failure {
                Text(failure)
            }
            Button("Sign in") {
                Task { await model.signIn() }
            }
        }
        .navigationTitle("Robin")
    }
}

private struct ConversationForm: View {
    @ObservedObject var model: ShellModel

    var body: some View {
        RobinFields {
            if model.waiting {
                Text("Waiting for Robin…")
            }
            switch model.phase {
            case .ready(let threads, let reply):
                Text(threads.isEmpty ? "No threads yet" : threads.joined(separator: ", "))
                if let reply {
                    Text(reply)
                }
                if !model.serverTiming.isEmpty {
                    Text(model.serverTiming)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            case .confirm(_, let prompt, let tool):
                Text(prompt)
                Text(tool)
                Button("Confirm") {
                    Task { await model.confirm() }
                }
                .buttonStyle(.borderedProminent)
                if let failure = model.failure {
                    Text(failure)
                }
            case .handoff(_, let prompt, let liveURL):
                Text(prompt)
                if !liveURL.isEmpty, let url = model.liveViewURL(path: liveURL) {
                    Link("Open live view", destination: url)
                }
                Button("Done") {
                    Task { await model.confirm() }
                }
                .buttonStyle(.borderedProminent)
                if let failure = model.failure {
                    Text(failure)
                }
            case .signedOut:
                EmptyView()
            }
            TextField("Thread", text: $model.conversationID)
                .robinField()
            TextField("Message", text: $model.draft)
                .robinField()
            Button("Send") {
                Task { await model.send() }
            }
            TextField("IMAP host, blank for iCloud", text: $model.imapHost)
                .robinField()
            TextField("SMTP host, blank for iCloud", text: $model.smtpHost)
                .robinField()
            TextField("Mail user", text: $model.mailUser)
                .robinField()
            SecureField("Mail password", text: $model.mailPassword)
                .robinField()
            Button("Connect mail") {
                Task { await model.connectMail() }
            }
            TextField("Calendar URL", text: $model.calendarURL)
                .robinField()
            TextField("Calendar user", text: $model.calendarUser)
                .robinField()
            SecureField("Calendar password", text: $model.calendarPassword)
                .robinField()
            Button("Connect calendar") {
                Task { await model.connectCalendar() }
            }
            Toggle("Check mail and calendar", isOn: $model.scheduleOn)
            Button("Save schedule") {
                Task { await model.saveSchedule() }
            }
            TextField("Given name", text: $model.givenName)
                .robinField()
            TextField("Family name", text: $model.familyName)
                .robinField()
            TextField("Email", text: $model.profileEmail)
                .robinField()
            TextField("Phone", text: $model.profilePhone)
                .robinField()
            TextField("Address", text: $model.profileAddress)
                .robinField()
            TextField("City", text: $model.profileCity)
                .robinField()
            TextField("Postal code", text: $model.profilePostalCode)
                .robinField()
            TextField("Country", text: $model.profileCountry)
                .robinField()
            Button("Save personal details") {
                Task { await model.saveProfile() }
            }
            SecureField("Vault passphrase", text: $model.vaultPassphrase)
                .robinField()
            TextField("Vault export", text: $model.vaultExport)
                .robinField()
            Button("Export vault") {
                Task { await model.exportVault() }
            }
            Button("Import vault") {
                Task { await model.importVault() }
            }
            Button("Leave this instance") {
                Task { await model.leave() }
            }
            if let failure = model.failure {
                Text(failure)
            }
        }
        .navigationTitle(model.accountID)
    }
}

private struct RobinFields<Content: View>: View {
    @ViewBuilder var content: () -> Content

    var body: some View {
        #if os(macOS)
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                content()
            }
            .padding(20)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        #else
        Form {
            content()
        }
        #endif
    }
}

private extension View {
    func robinField() -> some View {
        #if os(iOS)
        self.textInputAutocapitalization(.never).autocorrectionDisabled()
        #else
        self.textFieldStyle(.roundedBorder)
        #endif
    }
}

#if os(macOS)
private final class RobinActivation: NSObject, NSApplicationDelegate {
    nonisolated func applicationDidFinishLaunching(_ notification: Notification) {
        Task { @MainActor in
            NSApplication.shared.setActivationPolicy(.regular)
            NSApplication.shared.activate()
            for window in NSApplication.shared.windows {
                window.makeKeyAndOrderFront(nil)
            }
        }
    }
}
#endif

@main
struct RobinApp: App {
    #if os(macOS)
    @NSApplicationDelegateAdaptor(RobinActivation.self) private var activation
    #endif

    var body: some Scene {
        WindowGroup {
            RobinRootView()
        }
        #if os(macOS)
        .defaultSize(width: 480, height: 720)
        #endif
    }
}
