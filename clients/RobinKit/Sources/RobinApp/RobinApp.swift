import AuthenticationServices
import RobinKit
import SwiftUI
import UserNotifications

#if os(macOS)
import AppKit
#endif
#if os(iOS)
import BackgroundTasks
import UIKit
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
    @Published var inputValues: [String: String] = [:]
    @Published private(set) var attention: [AttentionItem] = []

    private let shell: Shell
    private var pollMirror: Task<Void, Never>?

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
            await AttentionAlerts.requestPermission()
            await syncAttention(alertFresh: false)
            startMirroringPoll()
            #if os(iOS)
            AttentionAlerts.scheduleBackgroundRefresh()
            #endif
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

    func submitInput() async {
        guard case .input(_, _, let request) = phase else { return }
        let values = inputValues
        inputValues = [:]
        await perform {
            try await self.shell.submitInput(
                conversationID: self.conversationID,
                requestID: request.requestID,
                values: values
            )
        }
    }

    func signIn(at url: URL, using session: WebAuthenticationSession) async {
        failure = nil
        do {
            let callback = try await session.authenticate(using: url, callbackURLScheme: robinOAuthScheme)
            inputValues["redirect"] = callback.absoluteString
            await submitInput()
        } catch let error as ASWebAuthenticationSessionError where error.code == .canceledLogin {
            return
        } catch {
            failure = error.localizedDescription
        }
    }

    func cancelInput() async {
        guard case .input(_, _, let request) = phase else { return }
        inputValues = [:]
        await perform {
            try await self.shell.cancelInput(
                conversationID: self.conversationID,
                requestID: request.requestID
            )
        }
    }

    func openAttention(_ item: AttentionItem) async {
        if !item.conversationID.isEmpty {
            conversationID = item.conversationID
        }
        await perform {
            try await self.shell.openNotification(item)
        }
        attention = await shell.notifications
    }

    func dismissAttention(_ item: AttentionItem) async {
        await perform {
            try await self.shell.ackNotifications([item.itemID])
        }
        attention = await shell.notifications
    }

    func pollAttention() async {
        await syncAttention(alertFresh: true)
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
        pollMirror?.cancel()
        pollMirror = nil
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
        attention = []
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
            attention = await shell.notifications
        } catch let error as RobinFailure {
            failure = error.message
        } catch {
            failure = _reach(error)
        }
    }

    private func syncAttention(alertFresh: Bool) async {
        do {
            _ = try await shell.refreshNotifications(markFresh: alertFresh)
            attention = await shell.notifications
            if alertFresh {
                let fresh = await shell.freshNotificationIDs
                let items = attention.filter { fresh.contains($0.itemID) }
                for item in items {
                    AttentionAlerts.post(item)
                }
            }
        } catch {
            // Poll failures stay quiet; the next tick retries.
        }
    }

    private func startMirroringPoll() {
        pollMirror?.cancel()
        pollMirror = Task {
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 30_000_000_000)
                guard !Task.isCancelled else { return }
                await syncAttention(alertFresh: true)
            }
        }
    }
}

private enum AttentionAlerts {
    static let refreshTaskID = "robin.attention.refresh"

    static func requestPermission() async {
        _ = try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge])
    }

    static func post(_ item: AttentionItem) {
        let content = UNMutableNotificationContent()
        content.title = "Robin"
        content.body = item.text
        content.sound = .default
        content.userInfo = [
            "notification_id": item.itemID,
            "conversation_id": item.conversationID,
            "kind": item.kind,
        ]
        let request = UNNotificationRequest(identifier: item.itemID, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request)
    }

    #if os(iOS)
    static func registerBackgroundRefresh() {
        BGTaskScheduler.shared.register(forTaskWithIdentifier: refreshTaskID, using: nil) { task in
            task.setTaskCompleted(success: true)
            scheduleBackgroundRefresh()
        }
    }

    static func scheduleBackgroundRefresh() {
        let request = BGAppRefreshTaskRequest(identifier: refreshTaskID)
        request.earliestBeginDate = Date(timeIntervalSinceNow: 15 * 60)
        try? BGTaskScheduler.shared.submit(request)
    }
    #endif
}

private func _reach(_ error: Error) -> String {
    if let urlError = error as? URLError {
        switch urlError.code {
        case .timedOut, .networkConnectionLost:
            return "Robin took too long to answer."
        default:
            break
        }
    }
    let text = String(describing: error).lowercased()
    if text.contains("timed out")
        || text.contains("timeout")
        || text.contains("network connection was lost")
        || text.contains("-1001")
        || text.contains("-1005")
    {
        return "Robin took too long to answer."
    }
    return "Could not reach Robin."
}

struct RobinRootView: View {
    @StateObject private var model = ShellModel()
    #if os(iOS)
    @Environment(\.scenePhase) private var scenePhase
    #endif

    var body: some View {
        NavigationStack {
            switch model.phase {
            case .signedOut:
                SignInForm(model: model)
            case .ready, .confirm, .handoff, .input:
                ConversationForm(model: model)
            }
        }
        #if os(iOS)
        .onChange(of: scenePhase) { _, phase in
            if phase == .active {
                Task { await model.pollAttention() }
            }
        }
        #endif
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
            if !model.attention.isEmpty {
                Text("Needs you").font(.headline)
                ForEach(model.attention) { item in
                    HStack {
                        Button(item.text) {
                            Task { await model.openAttention(item) }
                        }
                        .buttonStyle(.plain)
                        Spacer()
                        Button("Dismiss") {
                            Task { await model.dismissAttention(item) }
                        }
                    }
                }
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
            case .input(_, let prompt, let request):
                InputFormView(model: model, prompt: prompt, request: request)
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

/// Matches OAUTH_REDIRECT_URI on the Robin server.
private let robinOAuthScheme = "robin"

private struct InputFormView: View {
    @ObservedObject var model: ShellModel
    @Environment(\.webAuthenticationSession) private var webAuthenticationSession
    let prompt: String
    let request: InputRequest

    private var canSubmit: Bool {
        request.fields.allSatisfy { field in
            !field.required || !(model.inputValues[field.fieldID] ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(request.title).font(.headline)
            Text(prompt.isEmpty ? request.reason : prompt)
            ForEach(request.fields) { field in
                inputField(field)
            }
            HStack {
                Button("Cancel") {
                    Task { await model.cancelInput() }
                }
                if let raw = request.openURL, let url = URL(string: raw), !raw.isEmpty {
                    Button("Sign in") {
                        Task { await model.signIn(at: url, using: webAuthenticationSession) }
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(model.waiting)
                } else {
                    Button("Submit") {
                        Task { await model.submitInput() }
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(!canSubmit || model.waiting)
                }
            }
            if let failure = model.failure {
                Text(failure)
            }
        }
    }

    @ViewBuilder
    private func inputField(_ field: InputField) -> some View {
        let binding = Binding(
            get: { model.inputValues[field.fieldID] ?? "" },
            set: { model.inputValues[field.fieldID] = $0 }
        )
        Text(field.label)
        if field.kind == "secret" || field.kind == "otp" {
            SecureField(field.placeholder.isEmpty ? field.label : field.placeholder, text: binding)
                .robinField()
                #if os(iOS)
                .textContentType(field.kind == "otp" ? .oneTimeCode : .password)
                #endif
        } else if field.kind == "choice", !field.options.isEmpty {
            Picker(field.label, selection: binding) {
                Text("").tag("")
                ForEach(field.options, id: \.self) { option in
                    Text(option).tag(option)
                }
            }
        } else {
            TextField(field.placeholder.isEmpty ? field.label : field.placeholder, text: binding)
                .robinField()
                #if os(iOS)
                .textContentType(contentType(for: field.kind))
                .keyboardType(field.kind == "url" ? .URL : field.kind == "email" ? .emailAddress : field.kind == "number" ? .decimalPad : .default)
                #endif
        }
    }

    #if os(iOS)
    private func contentType(for kind: String) -> UITextContentType? {
        switch kind {
        case "username": return .username
        case "email": return .emailAddress
        case "url": return .URL
        case "otp": return .oneTimeCode
        default: return nil
        }
    }
    #endif
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

    init() {
        #if os(iOS)
        AttentionAlerts.registerBackgroundRefresh()
        #endif
    }

    var body: some Scene {
        WindowGroup {
            RobinRootView()
        }
        #if os(macOS)
        .defaultSize(width: 480, height: 720)
        #endif
    }
}
