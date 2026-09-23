import Foundation

/// What the Mac and iPhone screens show. Signing in replaces the instance on purpose.
public enum ShellPhase: Equatable, Sendable {
    case signedOut
    case ready(threads: [String], reply: String?)
    case confirm(threads: [String], prompt: String, tool: String)
}

public actor Shell {
    public private(set) var phase: ShellPhase = .signedOut
    private var client: RobinClient?
    private let transport: any RobinTransport

    public init(transport: any RobinTransport = HTTPTransport()) {
        self.transport = transport
    }

    public func signIn(instance: URL, accountID: String, password: String) async throws {
        let next = RobinClient(baseURL: instance, accountID: accountID, transport: transport)
        try await next.login(password: password)
        let threads = try await next.threads()
        client = next
        phase = .ready(threads: threads, reply: nil)
    }

    public func leave() {
        client = nil
        phase = .signedOut
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

    private func show(_ reply: Reply, on current: RobinClient) async throws {
        let threads = try await current.threads()
        if reply.status == "confirm" {
            phase = .confirm(threads: threads, prompt: reply.text, tool: reply.tool ?? "")
        } else {
            phase = .ready(threads: threads, reply: reply.text)
        }
    }
}
