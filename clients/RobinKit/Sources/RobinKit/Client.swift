import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

/// Talks to one Robin instance for one account. Text is sent as the person typed it. A model provider is not a destination.
public actor RobinClient {
    private let baseURL: URL
    private let accountID: String
    private let transport: any RobinTransport
    private var token: String?

    public init(baseURL: URL, accountID: String, transport: any RobinTransport = HTTPTransport()) {
        self.baseURL = baseURL
        self.accountID = accountID
        self.transport = transport
    }

    public func register(password: String) async throws {
        let body = try encode(AccountBody(accountID: accountID, password: password))
        let raw = try await transport.call(url: try url(path: "/v1/accounts"), method: "POST", body: body, token: nil)
        _ = try accepted(raw, status: 201)
    }

    public func login(password: String) async throws {
        let body = try encode(AccountBody(accountID: accountID, password: password))
        let raw = try await transport.call(url: try url(path: "/v1/sessions"), method: "POST", body: body, token: nil)
        let session = try JSONDecoder().decode(SessionBody.self, from: accepted(raw, status: 200))
        token = session.token
    }

    public func send(conversationID: String, text: String, allowCloud: Bool = false) async throws -> Reply {
        let body = try encode(
            MessageBody(accountID: accountID, conversationID: conversationID, text: text, confirm: nil, allowCloud: allowCloud)
        )
        return try await postMessage(body)
    }

    public func confirm(conversationID: String) async throws -> Reply {
        let body = try encode(
            MessageBody(accountID: accountID, conversationID: conversationID, text: nil, confirm: true, allowCloud: nil)
        )
        return try await postMessage(body)
    }

    public func submitInput(conversationID: String, requestID: String, values: [String: String]) async throws -> Reply {
        let body = try encode(
            InputMessageBody(
                accountID: accountID,
                conversationID: conversationID,
                input: InputPayload(requestID: requestID, values: values, cancel: nil)
            )
        )
        return try await postMessage(body)
    }

    public func cancelInput(conversationID: String, requestID: String) async throws -> Reply {
        let body = try encode(
            InputMessageBody(
                accountID: accountID,
                conversationID: conversationID,
                input: InputPayload(requestID: requestID, values: nil, cancel: true)
            )
        )
        return try await postMessage(body)
    }

    public func setSchedule(enabled: Bool) async throws {
        let body = try encode(ScheduleBody(accountID: accountID, enabled: enabled))
        let raw = try await transport.call(
            url: try url(path: "/v1/schedule"),
            method: "POST",
            body: body,
            token: try sessionToken()
        )
        _ = try accepted(raw, status: 200)
    }

    public func schedule() async throws -> Bool {
        let raw = try await transport.call(
            url: try url(path: "/v1/schedule", query: ["account_id": accountID]),
            method: "GET",
            body: nil,
            token: try sessionToken()
        )
        return try JSONDecoder().decode(ScheduleState.self, from: accepted(raw, status: 200)).enabled
    }

    public func notifications() async throws -> [AttentionItem] {
        let raw = try await transport.call(
            url: try url(path: "/v1/notifications", query: ["account_id": accountID]),
            method: "GET",
            body: nil,
            token: try sessionToken()
        )
        return try JSONDecoder().decode(NotificationList.self, from: accepted(raw, status: 200)).notifications
    }

    public func ackNotifications(_ ids: [String]) async throws -> Int {
        let body = try encode(NotificationAckBody(accountID: accountID, ack: ids))
        let raw = try await transport.call(
            url: try url(path: "/v1/notifications"),
            method: "POST",
            body: body,
            token: try sessionToken()
        )
        return try JSONDecoder().decode(NotificationAckResult.self, from: accepted(raw, status: 200)).acked
    }

    public func connect(name: String, secret: String) async throws {
        let body = try encode(SecretBody(accountID: accountID, name: name, value: secret))
        let raw = try await transport.call(
            url: try url(path: "/v1/secrets"),
            method: "POST",
            body: body,
            token: try sessionToken()
        )
        _ = try accepted(raw, status: 200)
    }

    public func exportVault(passphrase: String) async throws -> String {
        let body = try encode(ExportBody(accountID: accountID, passphrase: passphrase, export: nil))
        let raw = try await transport.call(
            url: try url(path: "/v1/export"),
            method: "POST",
            body: body,
            token: try sessionToken()
        )
        return try JSONDecoder().decode(ExportResult.self, from: accepted(raw, status: 200)).export
    }

    public func importVault(passphrase: String, export: String) async throws {
        let body = try encode(ExportBody(accountID: accountID, passphrase: passphrase, export: export))
        let raw = try await transport.call(
            url: try url(path: "/v1/import"),
            method: "POST",
            body: body,
            token: try sessionToken()
        )
        _ = try accepted(raw, status: 200)
    }

    public func connections() async throws -> [String] {
        let raw = try await transport.call(
            url: try url(path: "/v1/secrets", query: ["account_id": accountID]),
            method: "GET",
            body: nil,
            token: try sessionToken()
        )
        return try JSONDecoder().decode(SecretList.self, from: accepted(raw, status: 200)).connected
    }

    /// Read-only assistant snapshot: connectors, MCP, capabilities, schedule, threads.
    public func overview() async throws -> AssistantOverview {
        let raw = try await transport.call(
            url: try url(path: "/v1/status", query: ["account_id": accountID]),
            method: "GET",
            body: nil,
            token: try sessionToken()
        )
        return try JSONDecoder().decode(AssistantOverview.self, from: accepted(raw, status: 200))
    }

    public func profile() async throws -> [String: String] {
        let raw = try await transport.call(
            url: try url(path: "/v1/profile", query: ["account_id": accountID]),
            method: "GET",
            body: nil,
            token: try sessionToken()
        )
        return try JSONDecoder().decode(ProfileBody.self, from: accepted(raw, status: 200)).fields
    }

    public func saveProfile(_ fields: [String: String]) async throws -> [String: String] {
        let body = try encode(ProfileUpdate(accountID: accountID, fields: fields))
        let raw = try await transport.call(
            url: try url(path: "/v1/profile"),
            method: "POST",
            body: body,
            token: try sessionToken()
        )
        return try JSONDecoder().decode(ProfileBody.self, from: accepted(raw, status: 200)).fields
    }

    public func threads() async throws -> [String] {
        let raw = try await transport.call(
            url: try url(path: "/v1/threads", query: ["account_id": accountID]),
            method: "GET",
            body: nil,
            token: try sessionToken()
        )
        return try JSONDecoder().decode(ThreadList.self, from: accepted(raw, status: 200)).threads
    }

    public func turns(conversationID: String) async throws -> [Turn] {
        let raw = try await transport.call(
            url: try url(path: "/v1/threads/\(conversationID)", query: ["account_id": accountID]),
            method: "GET",
            body: nil,
            token: try sessionToken()
        )
        return try JSONDecoder().decode(ThreadTurns.self, from: accepted(raw, status: 200)).turns
    }

    public func deleteThread(conversationID: String) async throws {
        let raw = try await transport.call(
            url: try url(path: "/v1/threads/\(conversationID)", query: ["account_id": accountID]),
            method: "DELETE",
            body: nil,
            token: try sessionToken()
        )
        _ = try accepted(raw, status: 200)
    }

    /// After a send times out, poll the stored thread for Robin's reply (the server often finished anyway).
    public func awaitStoredReply(
        conversationID: String,
        afterUserText: String,
        attempts: Int = 36,
        delayNanoseconds: UInt64 = 5_000_000_000
    ) async throws -> Reply? {
        for attempt in 0..<attempts {
            if attempt > 0 {
                try await Task.sleep(nanoseconds: delayNanoseconds)
            }
            let turns = try await turns(conversationID: conversationID)
            if let reply = latestRobinReply(in: turns, afterUserText: afterUserText) {
                return reply
            }
        }
        return nil
    }

    private func postMessage(_ body: Data) async throws -> Reply {
        let raw = try await transport.call(
            url: try url(path: "/v1/messages"),
            method: "POST",
            body: body,
            token: try sessionToken()
        )
        return try JSONDecoder().decode(Reply.self, from: accepted(raw, status: 200))
    }

    private func url(path: String, query: [String: String] = [:]) throws -> URL {
        guard var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else {
            throw RobinFailure(status: 0, message: "bad instance url")
        }
        components.path = path
        if !query.isEmpty {
            components.queryItems = query.keys.sorted().map { URLQueryItem(name: $0, value: query[$0]) }
        }
        guard let built = components.url else {
            throw RobinFailure(status: 0, message: "bad instance url")
        }
        return built
    }

    private func sessionToken() throws -> String {
        guard let token else {
            throw RobinFailure(status: 401, message: "login required")
        }
        return token
    }
}

public struct Reply: Decodable, Sendable, Equatable {
    public let status: String
    public let text: String
    public let route: String
    public let tool: String?
    public let timing: String?
    public let liveURL: String?
    public let input: InputRequest?

    public init(
        status: String,
        text: String,
        route: String,
        tool: String?,
        timing: String? = nil,
        liveURL: String? = nil,
        input: InputRequest? = nil
    ) {
        self.status = status
        self.text = text
        self.route = route
        self.tool = tool
        self.timing = timing
        self.liveURL = liveURL
        self.input = input
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        status = try container.decode(String.self, forKey: .status)
        text = try container.decode(String.self, forKey: .text)
        route = try container.decode(String.self, forKey: .route)
        tool = try container.decodeIfPresent(String.self, forKey: .tool)
        timing = try container.decodeIfPresent(String.self, forKey: .timing)
        liveURL = try container.decodeIfPresent(String.self, forKey: .liveURL)
        input = try container.decodeIfPresent(InputRequest.self, forKey: .input)
    }

    private enum CodingKeys: String, CodingKey {
        case status, text, route, tool, timing, input
        case liveURL = "live_url"
    }
}

public struct InputRequest: Decodable, Sendable, Equatable {
    public let requestID: String
    public let title: String
    public let reason: String
    public let owner: String
    public let fields: [InputField]
    public let openURL: String?

    private enum CodingKeys: String, CodingKey {
        case requestID = "request_id"
        case title, reason, owner, fields
        case openURL = "open_url"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        requestID = try container.decode(String.self, forKey: .requestID)
        title = try container.decode(String.self, forKey: .title)
        reason = try container.decode(String.self, forKey: .reason)
        owner = try container.decode(String.self, forKey: .owner)
        fields = try container.decodeIfPresent([InputField].self, forKey: .fields) ?? []
        openURL = try container.decodeIfPresent(String.self, forKey: .openURL)
    }
}

public struct InputField: Decodable, Sendable, Equatable, Identifiable {
    public var id: String { fieldID }
    public let fieldID: String
    public let label: String
    public let kind: String
    public let required: Bool
    public let placeholder: String
    public let options: [String]

    private enum CodingKeys: String, CodingKey {
        case fieldID = "id"
        case label, kind, required, placeholder, options
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        fieldID = try container.decode(String.self, forKey: .fieldID)
        label = try container.decode(String.self, forKey: .label)
        kind = try container.decodeIfPresent(String.self, forKey: .kind) ?? "text"
        required = try container.decodeIfPresent(Bool.self, forKey: .required) ?? true
        placeholder = try container.decodeIfPresent(String.self, forKey: .placeholder) ?? ""
        options = try container.decodeIfPresent([String].self, forKey: .options) ?? []
    }
}

public struct AttentionItem: Decodable, Sendable, Equatable, Identifiable {
    public var id: String { itemID }
    public let itemID: String
    public let kind: String
    public let conversationID: String
    public let text: String
    public let created: String

    private enum CodingKeys: String, CodingKey {
        case itemID = "id"
        case kind, text, created
        case conversationID = "conversation_id"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        itemID = try container.decode(String.self, forKey: .itemID)
        kind = try container.decodeIfPresent(String.self, forKey: .kind) ?? "notify"
        conversationID = try container.decodeIfPresent(String.self, forKey: .conversationID) ?? ""
        text = try container.decode(String.self, forKey: .text)
        created = try container.decodeIfPresent(String.self, forKey: .created) ?? ""
    }

    public init(itemID: String, kind: String, conversationID: String, text: String, created: String = "") {
        self.itemID = itemID
        self.kind = kind
        self.conversationID = conversationID
        self.text = text
        self.created = created
    }
}

private struct NotificationList: Decodable {
    var notifications: [AttentionItem]
}

private struct NotificationAckBody: Encodable {
    var accountID: String
    var ack: [String]

    enum CodingKeys: String, CodingKey {
        case accountID = "account_id"
        case ack
    }
}

private struct NotificationAckResult: Decodable {
    var acked: Int
}

private struct InputMessageBody: Encodable {
    var accountID: String
    var conversationID: String
    var input: InputPayload

    enum CodingKeys: String, CodingKey {
        case accountID = "account_id"
        case conversationID = "conversation_id"
        case input
    }
}

private struct InputPayload: Encodable {
    var requestID: String
    var values: [String: String]?
    var cancel: Bool?

    enum CodingKeys: String, CodingKey {
        case requestID = "request_id"
        case values, cancel
    }
}

public struct RobinFailure: Error, Equatable, Sendable {
    public let status: Int
    public let message: String
}

public struct RobinRaw: Sendable {
    public var status: Int
    public var data: Data

    public init(status: Int, data: Data) {
        self.status = status
        self.data = data
    }
}

public protocol RobinTransport: Sendable {
    func call(url: URL, method: String, body: Data?, token: String?) async throws -> RobinRaw
}

public struct HTTPTransport: RobinTransport {
    private static let session: URLSession = {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 600
        configuration.timeoutIntervalForResource = 600
        return URLSession(configuration: configuration)
    }()

    public init() {}

    public func call(url: URL, method: String, body: Data?, token: String?) async throws -> RobinRaw {
        var request = URLRequest(url: url, timeoutInterval: 600)
        request.httpMethod = method
        if let token {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = body
        }
        let (data, response) = try await Self.session.data(for: request)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        return RobinRaw(status: status, data: data)
    }
}

private struct AccountBody: Encodable {
    var accountID: String
    var password: String

    enum CodingKeys: String, CodingKey {
        case accountID = "account_id"
        case password
    }
}

private struct MessageBody: Encodable {
    var accountID: String
    var conversationID: String
    var text: String?
    var confirm: Bool?
    var allowCloud: Bool?

    enum CodingKeys: String, CodingKey {
        case accountID = "account_id"
        case conversationID = "conversation_id"
        case text
        case confirm
        case allowCloud = "allow_cloud"
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(accountID, forKey: .accountID)
        try container.encode(conversationID, forKey: .conversationID)
        try container.encodeIfPresent(text, forKey: .text)
        try container.encodeIfPresent(confirm, forKey: .confirm)
        try container.encodeIfPresent(allowCloud, forKey: .allowCloud)
    }
}

private struct SessionBody: Decodable {
    var token: String
}

private struct ScheduleBody: Encodable {
    var accountID: String
    var enabled: Bool

    enum CodingKeys: String, CodingKey {
        case accountID = "account_id"
        case enabled
    }
}

private struct ScheduleState: Decodable {
    var enabled: Bool
}

private struct ExportBody: Encodable {
    var accountID: String
    var passphrase: String
    var export: String?

    enum CodingKeys: String, CodingKey {
        case accountID = "account_id"
        case passphrase
        case export
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(accountID, forKey: .accountID)
        try container.encode(passphrase, forKey: .passphrase)
        try container.encodeIfPresent(export, forKey: .export)
    }
}

private struct ExportResult: Decodable {
    var export: String
}

private struct SecretBody: Encodable {
    var accountID: String
    var name: String
    var value: String

    enum CodingKeys: String, CodingKey {
        case accountID = "account_id"
        case name
        case value
    }
}

private struct SecretList: Decodable {
    var connected: [String]
}

private struct ProfileUpdate: Encodable {
    var accountID: String
    var fields: [String: String]

    enum CodingKeys: String, CodingKey {
        case accountID = "account_id"
        case fields
    }
}

private struct ProfileBody: Decodable {
    var fields: [String: String]
}

private struct ThreadList: Decodable {
    var threads: [String]
}

public struct Turn: Decodable, Sendable, Equatable {
    public var role: String
    public var text: String
}

public struct AssistantOverview: Decodable, Sendable, Equatable {
    public var accountID: String
    public var scheduleEnabled: Bool
    public var connected: [String]
    public var profilePresent: [String]
    public var mcp: [McpServerSummary]
    public var capabilities: [CapabilitySummary]
    public var connectors: [String]
    public var threads: [String]
    public var privateInstance: PrivateInstanceSummary?

    private enum CodingKeys: String, CodingKey {
        case accountID = "account_id"
        case scheduleEnabled = "schedule_enabled"
        case connected
        case profilePresent = "profile_present"
        case mcp, capabilities, connectors, threads
        case privateInstance = "private"
    }
}

public struct McpServerSummary: Decodable, Sendable, Equatable {
    public var name: String
    public var status: String
    public var transport: String
    public var tools: Int

    public init(name: String, status: String, transport: String, tools: Int) {
        self.name = name
        self.status = status
        self.transport = transport
        self.tools = tools
    }
}

public struct CapabilitySummary: Decodable, Sendable, Equatable {
    public var id: String
    public var status: String
    public var tools: [String]

    public init(id: String, status: String, tools: [String]) {
        self.id = id
        self.status = status
        self.tools = tools
    }
}

public struct PrivateInstanceSummary: Decodable, Sendable, Equatable {
    public var httpsURL: String
    public var ready: Bool

    public init(httpsURL: String, ready: Bool) {
        self.httpsURL = httpsURL
        self.ready = ready
    }

    private enum CodingKeys: String, CodingKey {
        case httpsURL = "https_url"
        case ready
    }
}

private struct ThreadTurns: Decodable {
    var turns: [Turn]
}

func latestRobinReply(in turns: [Turn], afterUserText: String) -> Reply? {
    guard let userIndex = turns.lastIndex(where: { $0.role == "user" && $0.text == afterUserText }) else {
        return nil
    }
    let later = turns.suffix(from: turns.index(after: userIndex))
    guard let robin = later.last(where: { $0.role != "user" && !$0.text.isEmpty }) else {
        return nil
    }
    return Reply(status: robin.role, text: robin.text, route: "local", tool: nil)
}

private struct ErrorBody: Decodable {
    var error: String
}

private func encode(_ value: some Encodable) throws -> Data {
    try JSONEncoder().encode(value)
}

private func accepted(_ raw: RobinRaw, status: Int) throws -> Data {
    guard raw.status == status else {
        let message = (try? JSONDecoder().decode(ErrorBody.self, from: raw.data).error) ?? "request failed"
        throw RobinFailure(status: raw.status, message: message)
    }
    return raw.data
}
