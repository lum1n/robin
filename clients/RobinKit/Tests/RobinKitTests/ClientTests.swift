import Foundation
import Testing

@testable import RobinKit

actor ScriptedTransport: RobinTransport {
    struct Call: Sendable {
        var url: URL
        var method: String
        var body: Data?
        var token: String?
    }

    var calls: [Call] = []
    var responses: [RobinRaw]

    init(responses: [RobinRaw]) {
        self.responses = responses
    }

    func call(url: URL, method: String, body: Data?, token: String?) async throws -> RobinRaw {
        calls.append(Call(url: url, method: method, body: body, token: token))
        return responses.removeFirst()
    }
}

struct ClientTests {
    @Test func loginThenSendUsesTheSessionAndNotAModel() async throws {
        let transport = ScriptedTransport(responses: [
            RobinRaw(status: 200, data: Data(#"{"token":"sess-1"}"#.utf8)),
            RobinRaw(status: 200, data: Data(#"{"status":"reply","text":"hello","route":"local","tool":null}"#.utf8)),
        ])
        let client = RobinClient(
            baseURL: URL(string: "http://127.0.0.1:8787")!,
            accountID: "ada",
            transport: transport
        )
        try await client.login(password: "correct-horse")
        let reply = try await client.send(conversationID: "kitchen", text: "buy milk")
        #expect(reply == Reply(status: "reply", text: "hello", route: "local", tool: nil))

        let calls = await transport.calls
        #expect(calls[0].url.absoluteString == "http://127.0.0.1:8787/v1/sessions")
        #expect(calls[0].token == nil)
        #expect(String(decoding: calls[0].body ?? Data(), as: UTF8.self).contains("correct-horse"))
        #expect(calls[1].url.absoluteString == "http://127.0.0.1:8787/v1/messages")
        #expect(calls[1].token == "sess-1")
        let sent = String(decoding: calls[1].body ?? Data(), as: UTF8.self)
        #expect(sent.contains("\"account_id\":\"ada\""))
        #expect(sent.contains("buy milk"))
        #expect(!sent.contains("correct-horse"))
        #expect(!sent.contains("chat/completions"))
    }

    @Test func confirmAndThreadsStayOnThisAccount() async throws {
        let transport = ScriptedTransport(responses: [
            RobinRaw(status: 200, data: Data(#"{"token":"sess-1"}"#.utf8)),
            RobinRaw(status: 200, data: Data(#"{"status":"reply","text":"sent","route":"local","tool":"send_message"}"#.utf8)),
            RobinRaw(status: 200, data: Data(#"{"threads":["kitchen"]}"#.utf8)),
        ])
        let client = RobinClient(
            baseURL: URL(string: "https://robin-ada.exe.xyz")!,
            accountID: "ada",
            transport: transport
        )
        try await client.login(password: "pw")
        let reply = try await client.confirm(conversationID: "kitchen")
        let threads = try await client.threads()
        #expect(reply.tool == "send_message")
        #expect(threads == ["kitchen"])
        let calls = await transport.calls
        let confirm = String(decoding: calls[1].body ?? Data(), as: UTF8.self)
        #expect(confirm.contains("\"confirm\":true"))
        #expect(!confirm.contains("pw"))
        #expect(calls[2].url.absoluteString == "https://robin-ada.exe.xyz/v1/threads?account_id=ada")
        #expect(calls[2].token == "sess-1")
    }

    @Test func scheduleStaysOffUntilThisAccountTurnsItOn() async throws {
        let transport = ScriptedTransport(responses: [
            RobinRaw(status: 200, data: Data(#"{"token":"sess-1"}"#.utf8)),
            RobinRaw(status: 200, data: Data(#"{"enabled":false}"#.utf8)),
            RobinRaw(status: 200, data: Data(#"{"enabled":true}"#.utf8)),
        ])
        let client = RobinClient(
            baseURL: URL(string: "http://127.0.0.1:8787")!,
            accountID: "ada",
            transport: transport
        )
        try await client.login(password: "correct-horse")
        let before = try await client.schedule()
        try await client.setSchedule(enabled: true)
        #expect(before == false)
        let calls = await transport.calls
        #expect(calls[1].url.absoluteString == "http://127.0.0.1:8787/v1/schedule?account_id=ada")
        let posted = String(decoding: calls[2].body ?? Data(), as: UTF8.self)
        #expect(posted.contains("\"enabled\":true"))
        #expect(calls[2].token == "sess-1")
        #expect(!posted.contains("correct-horse"))
    }

    @Test func exportSendsThePassphraseOnce() async throws {
        let transport = ScriptedTransport(responses: [
            RobinRaw(status: 200, data: Data(#"{"token":"sess-1"}"#.utf8)),
            RobinRaw(status: 200, data: Data(#"{"export":"sealed-bundle"}"#.utf8)),
            RobinRaw(status: 200, data: Data(#"{"status":"reply","text":"ok","route":"local","tool":null}"#.utf8)),
        ])
        let client = RobinClient(
            baseURL: URL(string: "http://127.0.0.1:8787")!,
            accountID: "ada",
            transport: transport
        )
        try await client.login(password: "correct-horse")
        let bundle = try await client.exportVault(passphrase: "vault-passphrase-ada")
        #expect(bundle == "sealed-bundle")
        _ = try await client.send(conversationID: "home", text: "hello")
        let calls = await transport.calls
        let posted = String(decoding: calls[1].body ?? Data(), as: UTF8.self)
        #expect(calls[1].url.absoluteString == "http://127.0.0.1:8787/v1/export")
        #expect(posted.contains("vault-passphrase-ada"))
        let sent = String(decoding: calls[2].body ?? Data(), as: UTF8.self)
        #expect(!sent.contains("vault-passphrase-ada"))
        #expect(!sent.contains("correct-horse"))
    }

    @Test func connectSendsTheSecretOnceAndDoesNotKeepIt() async throws {
        let transport = ScriptedTransport(responses: [
            RobinRaw(status: 200, data: Data(#"{"token":"sess-1"}"#.utf8)),
            RobinRaw(status: 200, data: Data(#"{"name":"mailbox","connected":true}"#.utf8)),
            RobinRaw(status: 200, data: Data(#"{"connected":["mailbox"]}"#.utf8)),
        ])
        let client = RobinClient(
            baseURL: URL(string: "http://127.0.0.1:8787")!,
            accountID: "ada",
            transport: transport
        )
        try await client.login(password: "correct-horse")
        try await client.connect(name: "mailbox", secret: "mailbox-password-ada")
        let connected = try await client.connections()
        #expect(connected == ["mailbox"])
        let calls = await transport.calls
        let posted = String(decoding: calls[1].body ?? Data(), as: UTF8.self)
        #expect(calls[1].token == "sess-1")
        #expect(posted.contains("mailbox-password-ada"))
        #expect(calls[2].url.absoluteString == "http://127.0.0.1:8787/v1/secrets?account_id=ada")
        let listed = String(decoding: calls[2].body ?? Data(), as: UTF8.self)
        #expect(!listed.contains("mailbox-password-ada"))
    }

    @Test func profileRoundTripsWithoutLeakingIntoOtherCalls() async throws {
        let transport = ScriptedTransport(responses: [
            RobinRaw(status: 200, data: Data(#"{"token":"sess-1"}"#.utf8)),
            RobinRaw(status: 200, data: Data(#"{"fields":{"email":"ada@example.com","given_name":"Ada","family_name":"","full_name":"Ada","phone":"","address":"","city":"","postal_code":"","country":""},"present":["given_name","full_name","email"]}"#.utf8)),
            RobinRaw(status: 200, data: Data(#"{"fields":{"email":"ada@example.com","given_name":"Ada","family_name":"Lovelace","full_name":"Ada Lovelace","phone":"+47 900 00 000","address":"","city":"","postal_code":"","country":""},"present":["given_name","family_name","full_name","email","phone"]}"#.utf8)),
        ])
        let client = RobinClient(
            baseURL: URL(string: "http://127.0.0.1:8787")!,
            accountID: "ada",
            transport: transport
        )
        try await client.login(password: "correct-horse")
        let loaded = try await client.profile()
        #expect(loaded["email"] == "ada@example.com")
        let saved = try await client.saveProfile([
            "given_name": "Ada",
            "family_name": "Lovelace",
            "email": "ada@example.com",
            "phone": "+47 900 00 000",
        ])
        #expect(saved["full_name"] == "Ada Lovelace")
        let calls = await transport.calls
        #expect(calls[1].url.absoluteString == "http://127.0.0.1:8787/v1/profile?account_id=ada")
        #expect(calls[2].url.absoluteString == "http://127.0.0.1:8787/v1/profile")
        let posted = String(decoding: calls[2].body ?? Data(), as: UTF8.self)
        #expect(posted.contains("ada@example.com"))
        #expect(calls[2].token == "sess-1")
    }

    @Test func aMessageBeforeLoginFailsLocally() async throws {
        let transport = ScriptedTransport(responses: [])
        let client = RobinClient(
            baseURL: URL(string: "http://127.0.0.1:8787")!,
            accountID: "ada",
            transport: transport
        )
        await #expect(throws: RobinFailure(status: 401, message: "login required")) {
            try await client.send(conversationID: "kitchen", text: "hello")
        }
        #expect(await transport.calls.isEmpty)
    }

    @Test func theShellDoesNotContainAModelOrARedactor() throws {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Sources/RobinKit")
        let files = try FileManager.default.contentsOfDirectory(at: root, includingPropertiesForKeys: nil)
        let source = try files.map { try String(contentsOf: $0, encoding: .utf8) }.joined(separator: "\n")
        #expect(!source.contains("chat/completions"))
        #expect(!source.contains("openai"))
        #expect(!source.contains("redact"))
        #expect(!source.contains("gliner"))
    }
}
