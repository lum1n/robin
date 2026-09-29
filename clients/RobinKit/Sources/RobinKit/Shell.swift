import Foundation

/// What the Mac and iPhone screens show. Signing in replaces the instance on purpose.
public enum ShellPhase: Equatable, Sendable {
    case signedOut
    case ready(threads: [String], reply: String?)
    case confirm(threads: [String], prompt: String, tool: String)
}

public actor Shell {
    public private(set) var phase: ShellPhase = .signedOut
    public private(set) var scheduleEnabled = false
    public private(set) var serverTiming = ""
    private var client: RobinClient?
    private let transport: any RobinTransport

    public init(transport: any RobinTransport = HTTPTransport()) {
        self.transport = transport
    }

    public func signIn(instance: URL, accountID: String, password: String) async throws {
        let next = RobinClient(baseURL: instance, accountID: accountID, transport: transport)
        try await next.login(password: password)
        let threads = try await next.threads()
        scheduleEnabled = try await next.schedule()
        client = next
        phase = .ready(threads: threads, reply: nil)
    }

    public func leave() {
        client = nil
        scheduleEnabled = false
        serverTiming = ""
        phase = .signedOut
    }

    public func connectMailbox(imapHost: String, smtpHost: String, user: String, password: String) async throws {
        let current = try signedIn()
        try await current.connect(name: "mailbox", secret: json(["imap_host": imapHost, "password": password, "smtp_host": smtpHost, "user": user]))
    }

    public func connectCalendar(url: String, user: String, password: String) async throws {
        let current = try signedIn()
        try await current.connect(name: "calendar", secret: json(["password": password, "url": url, "user": user]))
    }

    public func exportVault(passphrase: String) async throws -> String {
        try await signedIn().exportVault(passphrase: passphrase)
    }

    public func importVault(passphrase: String, export: String) async throws {
        try await signedIn().importVault(passphrase: passphrase, export: export)
    }

    public func setSchedule(enabled: Bool) async throws {
        let current = try signedIn()
        try await current.setSchedule(enabled: enabled)
        scheduleEnabled = enabled
    }

    public func profile() async throws -> [String: String] {
        try await signedIn().profile()
    }

    public func saveProfile(_ fields: [String: String]) async throws -> [String: String] {
        try await signedIn().saveProfile(fields)
    }

    public func send(conversationID: String, text: String) async throws {
        let current = try signedIn()
        let reply = try await current.send(conversationID: conversationID, text: text)
        try await show(reply, on: current)
    }

    public func confirm(conversationID: String) async throws {
        let current = try signedIn()
        let reply = try await current.confirm(conversationID: conversationID)
        try await show(reply, on: current)
    }

    private func signedIn() throws -> RobinClient {
        guard let client else {
            throw RobinFailure(status: 401, message: "login required")
        }
        return client
    }

    private func json(_ fields: [String: String]) throws -> String {
        let data = try JSONSerialization.data(withJSONObject: fields, options: [.sortedKeys])
        return String(decoding: data, as: UTF8.self)
    }

    private func show(_ reply: Reply, on current: RobinClient) async throws {
        let threads = try await current.threads()
        serverTiming = reply.timing ?? ""
        if reply.status == "confirm" {
            phase = .confirm(threads: threads, prompt: reply.text, tool: reply.tool ?? "")
        } else {
            phase = .ready(threads: threads, reply: reply.text)
        }
    }
}
