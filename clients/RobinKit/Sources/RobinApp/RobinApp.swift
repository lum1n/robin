import RobinKit
import SwiftUI

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
    @Published var vaultPassphrase = ""
    @Published var vaultExport = ""
    @Published var failure: String?

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
        let secret = password
        password = ""
        do {
            try await shell.signIn(instance: url, accountID: accountID, password: secret)
            scheduleOn = await shell.scheduleEnabled
            phase = await shell.phase
        } catch let error as RobinFailure {
            failure = error.message
        } catch {
            failure = "Could not reach Robin."
        }
    }

    func send() async {
        let text = draft
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

    func exportVault() async {
        let secret = vaultPassphrase
        vaultPassphrase = ""
        failure = nil
        do {
            vaultExport = try await shell.exportVault(passphrase: secret)
            phase = await shell.phase
        } catch let error as RobinFailure {
            failure = error.message
        } catch {
            failure = "Could not reach Robin."
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
        failure = nil
        phase = .signedOut
    }

    private func perform(_ work: () async throws -> Void) async {
        failure = nil
        do {
            try await work()
            phase = await shell.phase
        } catch let error as RobinFailure {
            failure = error.message
        } catch {
            failure = "Could not reach Robin."
        }
    }
}

struct RobinRootView: View {
    @StateObject private var model = ShellModel()

    var body: some View {
        NavigationStack {
            switch model.phase {
            case .signedOut:
                SignInForm(model: model)
            case .ready, .confirm:
                ConversationForm(model: model)
            }
        }
    }
}

private struct SignInForm: View {
    @ObservedObject var model: ShellModel

    var body: some View {
        Form {
            TextField("Instance", text: $model.instance)
                .robinField()
            TextField("Account", text: $model.accountID)
                .robinField()
            SecureField("Password", text: $model.password)
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
        Form {
            switch model.phase {
            case .ready(let threads, let reply):
                Text(threads.isEmpty ? "No threads yet" : threads.joined(separator: ", "))
                if let reply {
                    Text(reply)
                }
            case .confirm(_, let prompt, let tool):
                Text(prompt)
                Text(tool)
                Button("Confirm") {
                    Task { await model.confirm() }
                }
            case .signedOut:
                EmptyView()
            }
            TextField("Thread", text: $model.conversationID)
                .robinField()
            TextField("Message", text: $model.draft)
            Button("Send") {
                Task { await model.send() }
            }
            TextField("IMAP host", text: $model.imapHost)
                .robinField()
            TextField("SMTP host", text: $model.smtpHost)
                .robinField()
            TextField("Mail user", text: $model.mailUser)
                .robinField()
            SecureField("Mail password", text: $model.mailPassword)
            Button("Connect mail") {
                Task { await model.connectMail() }
            }
            TextField("Calendar URL", text: $model.calendarURL)
                .robinField()
            TextField("Calendar user", text: $model.calendarUser)
                .robinField()
            SecureField("Calendar password", text: $model.calendarPassword)
            Button("Connect calendar") {
                Task { await model.connectCalendar() }
            }
            Toggle("Check mail and calendar", isOn: $model.scheduleOn)
            Button("Save schedule") {
                Task { await model.saveSchedule() }
            }
            SecureField("Vault passphrase", text: $model.vaultPassphrase)
            TextField("Vault export", text: $model.vaultExport)
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

private extension View {
    func robinField() -> some View {
        #if os(iOS)
        self.textInputAutocapitalization(.never).autocorrectionDisabled()
        #else
        self.autocorrectionDisabled()
        #endif
    }
}

@main
struct RobinApp: App {
    var body: some Scene {
        WindowGroup {
            RobinRootView()
        }
    }
}
