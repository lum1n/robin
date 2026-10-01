import Foundation

/// What the Mac and iPhone screens show. Signing in replaces the instance on purpose.
public enum ShellPhase: Equatable, Sendable {
    case signedOut
    case ready(threads: [String], reply: String?)
    case confirm(threads: [String], prompt: String, tool: String)
    case handoff(threads: [String], prompt: String, liveURL: String)
    case input(threads: [String], prompt: String, request: InputRequest)
}

public actor Shell {
    public private(set) var phase: ShellPhase = .signedOut
    public private(set) var scheduleEnabled = false
    public private(set) var serverTiming = ""
    public private(set) var notifications: [AttentionItem] = []
    /// IDs newly seen on the last poll (for local OS alerts).
    public private(set) var freshNotificationIDs: [String] = []
    private var client: RobinClient?
    private let transport: any RobinTransport
    private var knownNotificationIDs: Set<String> = []

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
        knownNotificationIDs = []
        notifications = []
        freshNotificationIDs = []
    }

    public func leave() {
        client = nil
        scheduleEnabled = false
        serverTiming = ""
        notifications = []
        freshNotificationIDs = []
        knownNotificationIDs = []
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

    /// Read-only assistant snapshot for the signed-in account.
    public func overview() async throws -> AssistantOverview {
        try await signedIn().overview()
    }

    /// Raw JSON call to a `/v1/` route for the signed-in account (for screens such as MCP setup).
    public func request(method: String, path: String, json: Data? = nil) async throws -> Data {
        try await signedIn().request(method: method, path: path, json: json)
    }

    /// Thread ids for the signed-in account (empty when signed out throws via signedIn()).
    public func listThreads() async throws -> [String] {
        try await signedIn().threads()
    }

    /// Full turn history for one conversation id.
    public func loadTurns(conversationID: String) async throws -> [Turn] {
        try await signedIn().turns(conversationID: conversationID)
    }

    /// Delete a conversation on the house (turns, vault, pending, thread row).
    public func deleteThread(conversationID: String) async throws {
        try await signedIn().deleteThread(conversationID: conversationID)
    }

    public func send(conversationID: String, text: String) async throws {
        let current = try signedIn()
        do {
            let reply = try await current.send(conversationID: conversationID, text: text)
            try await show(reply, on: current)
        } catch {
            guard isDroppedWhileWaiting(error) else { throw error }
            // Long browser turns often finish on the server after the phone drops the socket.
            if let recovered = try await current.awaitStoredReply(
                conversationID: conversationID,
                afterUserText: text
            ) {
                try await show(recovered, on: current)
                return
            }
            throw error
        }
    }

    public func confirm(conversationID: String) async throws {
        let current = try signedIn()
        do {
            let reply = try await current.confirm(conversationID: conversationID)
            try await show(reply, on: current)
        } catch {
            guard isDroppedWhileWaiting(error) else { throw error }
            let turns = try await current.turns(conversationID: conversationID)
            if let robin = turns.last(where: { $0.role == "reply" && !$0.text.isEmpty }) {
                try await show(
                    Reply(status: robin.role, text: robin.text, route: "local", tool: nil),
                    on: current
                )
                return
            }
            throw error
        }
    }

    public func submitInput(conversationID: String, requestID: String, values: [String: String]) async throws {
        let current = try signedIn()
        let reply = try await current.submitInput(conversationID: conversationID, requestID: requestID, values: values)
        try await show(reply, on: current)
    }

    public func cancelInput(conversationID: String, requestID: String) async throws {
        let current = try signedIn()
        let reply = try await current.cancelInput(conversationID: conversationID, requestID: requestID)
        try await show(reply, on: current)
    }

    @discardableResult
    public func refreshNotifications(markFresh: Bool = true) async throws -> [AttentionItem] {
        let current = try signedIn()
        let items = try await current.notifications()
        let ids = Set(items.map(\.itemID))
        if markFresh {
            freshNotificationIDs = items.map(\.itemID).filter { !knownNotificationIDs.contains($0) }
        } else {
            freshNotificationIDs = []
        }
        knownNotificationIDs = ids
        notifications = items
        return items
    }

    public func ackNotifications(_ ids: [String]) async throws {
        guard !ids.isEmpty else { return }
        let current = try signedIn()
        _ = try await current.ackNotifications(ids)
        knownNotificationIDs.subtract(ids)
        notifications.removeAll { ids.contains($0.itemID) }
        freshNotificationIDs.removeAll { ids.contains($0) }
    }

    public func openNotification(_ item: AttentionItem) async throws {
        try await ackNotifications([item.itemID])
        let current = try signedIn()
        let threads = try await current.threads()
        if item.kind == "confirm" {
            phase = .confirm(threads: threads, prompt: item.text, tool: "")
        } else {
            phase = .ready(threads: threads, reply: item.text)
        }
    }

    /// Phone/NAT paths often sever a long /v1/messages wait as timedOut (-1001) or
    /// networkConnectionLost (-1005) while the house server keeps working.
    private func isDroppedWhileWaiting(_ error: Error) -> Bool {
        if let urlError = error as? URLError {
            switch urlError.code {
            case .timedOut, .networkConnectionLost:
                return true
            default:
                break
            }
        }
        let text = String(describing: error).lowercased()
        return text.contains("timed out")
            || text.contains("timeout")
            || text.contains("network connection was lost")
            || text.contains("-1001")
            || text.contains("-1005")
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
        } else if reply.status == "handoff" {
            phase = .handoff(threads: threads, prompt: reply.text, liveURL: reply.liveURL ?? "")
        } else if reply.status == "input", let request = reply.input {
            phase = .input(threads: threads, prompt: reply.text, request: request)
        } else {
            phase = .ready(threads: threads, reply: reply.text)
        }
    }
}
