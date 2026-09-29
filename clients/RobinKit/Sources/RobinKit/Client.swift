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

    public init(status: String, text: String, route: String, tool: String?, timing: String? = nil) {
        self.status = status
        self.text = text
        self.route = route
        self.tool = tool
        self.timing = timing
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        status = try container.decode(String.self, forKey: .status)
        text = try container.decode(String.self, forKey: .text)
        route = try container.decode(String.self, forKey: .route)
        tool = try container.decodeIfPresent(String.self, forKey: .tool)
        timing = try container.decodeIfPresent(String.self, forKey: .timing)
    }

    private enum CodingKeys: String, CodingKey {
        case status, text, route, tool, timing
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
    public init() {}

    public func call(url: URL, method: String, body: Data?, token: String?) async throws -> RobinRaw {
        var request = URLRequest(url: url)
        request.httpMethod = method
        if let token {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = body
        }
        request.timeoutInterval = 300
        let (data, response) = try await URLSession.shared.data(for: request)
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
