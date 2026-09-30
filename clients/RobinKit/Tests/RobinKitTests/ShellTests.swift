import Foundation
import Testing

@testable import RobinKit

struct ShellTests {
    @Test func listThreadsAndLoadTurnsRequireSignIn() async throws {
        let transport = ScriptedTransport(responses: [])
        let shell = Shell(transport: transport)
        await #expect(throws: RobinFailure(status: 401, message: "login required")) {
            try await shell.listThreads()
        }
        await #expect(throws: RobinFailure(status: 401, message: "login required")) {
            try await shell.loadTurns(conversationID: "home")
        }
        await #expect(throws: RobinFailure(status: 401, message: "login required")) {
            try await shell.overview()
        }
        #expect(await transport.calls.isEmpty)
    }

    @Test func overviewReturnsAssistantSnapshotWithoutMutatingPhase() async throws {
        let payload = """
        {"account_id":"ada","schedule_enabled":true,"connected":["mailbox"],"profile_present":["email"],\
        "mcp":[{"name":"notes","status":"active","transport":"http","tools":2}],\
        "capabilities":[{"id":"post","status":"mail: connected","tools":["mail_list"]}],\
        "connectors":["mail: connected"],"threads":["home"],"private":null}
        """
        let transport = ScriptedTransport(responses: [
            raw(200, #"{"token":"sess-1"}"#),
            raw(200, #"{"threads":["home"]}"#),
            raw(200, #"{"enabled":false}"#),
            raw(200, payload),
        ])
        let shell = Shell(transport: transport)
        try await shell.signIn(instance: URL(string: "http://127.0.0.1:8787")!, accountID: "ada", password: "pw")
        let overview = try await shell.overview()
        #expect(overview.accountID == "ada")
        #expect(overview.scheduleEnabled == true)
        #expect(overview.connected == ["mailbox"])
        #expect(overview.profilePresent == ["email"])
        #expect(overview.mcp == [McpServerSummary(name: "notes", status: "active", transport: "http", tools: 2)])
        #expect(overview.capabilities == [CapabilitySummary(id: "post", status: "mail: connected", tools: ["mail_list"])])
        #expect(overview.connectors == ["mail: connected"])
        #expect(overview.threads == ["home"])
        #expect(overview.privateInstance == nil)
        #expect(await shell.phase == .ready(threads: ["home"], reply: nil))
        let calls = await transport.calls
        #expect(calls[3].url.absoluteString == "http://127.0.0.1:8787/v1/status?account_id=ada")
        #expect(calls[3].method == "GET")
    }

    @Test func listThreadsReturnsIdsWithoutMutatingPhase() async throws {
        let transport = ScriptedTransport(responses: [
            raw(200, #"{"token":"sess-1"}"#),
            raw(200, #"{"threads":["home"]}"#),
            raw(200, #"{"enabled":false}"#),
            raw(200, #"{"threads":["home","desk"]}"#),
        ])
        let shell = Shell(transport: transport)
        try await shell.signIn(instance: URL(string: "http://127.0.0.1:8787")!, accountID: "ada", password: "pw")
        #expect(await shell.phase == .ready(threads: ["home"], reply: nil))
        let threads = try await shell.listThreads()
        #expect(threads == ["home", "desk"])
        #expect(await shell.phase == .ready(threads: ["home"], reply: nil))
        let calls = await transport.calls
        #expect(calls[3].url.absoluteString == "http://127.0.0.1:8787/v1/threads?account_id=ada")
        #expect(calls[3].method == "GET")
        #expect(calls[3].token == "sess-1")
    }

    @Test func loadTurnsReturnsDecodedHistory() async throws {
        let transport = ScriptedTransport(responses: [
            raw(200, #"{"token":"sess-1"}"#),
            raw(200, #"{"threads":["home"]}"#),
            raw(200, #"{"enabled":false}"#),
            raw(200, #"{"turns":[{"role":"user","text":"hi"},{"role":"reply","text":"hello"}]}"#),
        ])
        let shell = Shell(transport: transport)
        try await shell.signIn(instance: URL(string: "http://127.0.0.1:8787")!, accountID: "ada", password: "pw")
        let turns = try await shell.loadTurns(conversationID: "home")
        #expect(turns == [
            Turn(role: "user", text: "hi"),
            Turn(role: "reply", text: "hello"),
        ])
        #expect(await shell.phase == .ready(threads: ["home"], reply: nil))
        let calls = await transport.calls
        #expect(calls[3].url.absoluteString == "http://127.0.0.1:8787/v1/threads/home?account_id=ada")
        #expect(calls[3].method == "GET")
        #expect(calls[3].token == "sess-1")
    }

    @Test(arguments: [URLError.Code.timedOut, .networkConnectionLost])
    func sendRecoversStoredReplyAfterDrop(_ drop: URLError.Code) async throws {
        actor RecoverTransport: RobinTransport {
            var messageAttempts = 0
            var calls = 0
            var responses: [RobinRaw]
            let drop: URLError.Code
            init(responses: [RobinRaw], drop: URLError.Code) {
                self.responses = responses
                self.drop = drop
            }
            func call(url: URL, method: String, body: Data?, token: String?) async throws -> RobinRaw {
                calls += 1
                if url.path.hasSuffix("/v1/messages") {
                    messageAttempts += 1
                    if messageAttempts == 1 {
                        throw URLError(drop)
                    }
                }
                return responses.removeFirst()
            }
        }
        let transport = RecoverTransport(
            responses: [
                raw(200, #"{"token":"sess-1"}"#),
                raw(200, #"{"threads":["home"]}"#),
                raw(200, #"{"enabled":false}"#),
                raw(200, #"{"turns":[{"role":"user","text":"find cars"},{"role":"reply","text":"Found GLC listings."}]}"#),
                raw(200, #"{"threads":["home"]}"#),
            ],
            drop: drop
        )
        let shell = Shell(transport: transport)
        try await shell.signIn(instance: URL(string: "http://127.0.0.1:8787")!, accountID: "ada", password: "pw")
        try await shell.send(conversationID: "home", text: "find cars")
        #expect(await shell.phase == .ready(threads: ["home"], reply: "Found GLC listings."))
    }

    @Test func aConfirmWaitsAndLeavingDropsTheSession() async throws {
        let transport = ScriptedTransport(responses: [
            raw(200, #"{"token":"sess-1"}"#),
            raw(200, #"{"threads":["home"]}"#),
            raw(200, #"{"enabled":false}"#),
            raw(200, #"{"status":"confirm","text":"Confirm send_message before Robin does it.","route":"local","tool":"send_message"}"#),
            raw(200, #"{"threads":["home"]}"#),
            raw(200, #"{"status":"reply","text":"sent","route":"local","tool":"send_message"}"#),
            raw(200, #"{"threads":["home"]}"#),
        ])
        let shell = Shell(transport: transport)
        try await shell.signIn(instance: URL(string: "http://127.0.0.1:8787")!, accountID: "ada", password: "pw")
        #expect(await shell.phase == .ready(threads: ["home"], reply: nil))
        try await shell.send(conversationID: "home", text: "send the note")
        #expect(await shell.phase == .confirm(threads: ["home"], prompt: "Confirm send_message before Robin does it.", tool: "send_message"))
        try await shell.confirm(conversationID: "home")
        #expect(await shell.phase == .ready(threads: ["home"], reply: "sent"))

        let calls = await transport.calls
        #expect(calls[2].url.absoluteString == "http://127.0.0.1:8787/v1/schedule?account_id=ada")
        #expect(calls[3].url.absoluteString == "http://127.0.0.1:8787/v1/messages")
        #expect(!String(decoding: calls[3].body ?? Data(), as: UTF8.self).contains("pw"))
        let confirm = String(decoding: calls[5].body ?? Data(), as: UTF8.self)
        #expect(confirm.contains("\"confirm\":true"))

        await shell.leave()
        #expect(await shell.phase == .signedOut)
        await #expect(throws: RobinFailure(status: 401, message: "login required")) {
            try await shell.send(conversationID: "home", text: "again")
        }
        #expect(await transport.calls.count == 7)
    }

    @Test func switchingInstanceUsesTheNewAddress() async throws {
        let transport = ScriptedTransport(responses: [
            raw(200, #"{"token":"house"}"#),
            raw(200, #"{"threads":[]}"#),
            raw(200, #"{"enabled":false}"#),
            raw(200, #"{"token":"private"}"#),
            raw(200, #"{"threads":["desk"]}"#),
            raw(200, #"{"enabled":false}"#),
            raw(200, #"{"status":"reply","text":"hello","route":"local","tool":null}"#),
            raw(200, #"{"threads":["desk"]}"#),
        ])
        let shell = Shell(transport: transport)
        try await shell.signIn(instance: URL(string: "http://127.0.0.1:8787")!, accountID: "ada", password: "pw")
        await shell.leave()
        try await shell.signIn(instance: URL(string: "https://robin-ada.exe.xyz")!, accountID: "ada", password: "other")
        try await shell.send(conversationID: "desk", text: "hello")
        let calls = await transport.calls
        #expect(calls[0].url.host == "127.0.0.1")
        #expect(calls[3].url.host == "robin-ada.exe.xyz")
        #expect(calls[6].url.host == "robin-ada.exe.xyz")
        #expect(calls[6].token == "private")
        #expect(await shell.phase == .ready(threads: ["desk"], reply: "hello"))
    }

    @Test func notificationsPollAndAck() async throws {
        let transport = ScriptedTransport(responses: [
            raw(200, #"{"token":"sess-1"}"#),
            raw(200, #"{"threads":["home"]}"#),
            raw(200, #"{"enabled":false}"#),
            raw(200, #"{"notifications":[{"id":"n1","kind":"confirm","conversation_id":"schedule","text":"Confirm send.","created":"2026-09-30T00:00:00Z"}]}"#),
            raw(200, #"{"acked":1}"#),
        ])
        let shell = Shell(transport: transport)
        try await shell.signIn(instance: URL(string: "http://127.0.0.1:8787")!, accountID: "ada", password: "pw")
        #expect(await shell.notifications.isEmpty)
        _ = try await shell.refreshNotifications(markFresh: true)
        let items = await shell.notifications
        #expect(items.count == 1)
        #expect(items[0].itemID == "n1")
        #expect(await shell.freshNotificationIDs == ["n1"])
        try await shell.ackNotifications(["n1"])
        #expect(await shell.notifications.isEmpty)
        let calls = await transport.calls
        #expect(calls.contains(where: { $0.url.path.hasSuffix("/v1/notifications") && $0.method == "GET" }))
        let ack = calls.last(where: { $0.method == "POST" && $0.url.path.hasSuffix("/v1/notifications") })
        let ackBody = String(decoding: ack?.body ?? Data(), as: UTF8.self)
        #expect(ackBody.contains("\"n1\""))
    }

    @Test func inputFormSubmitsAndCancels() async throws {
        let inputJSON = #"{"status":"input","text":"Enter the token.","route":"local","tool":null,"input":{"request_id":"req-1","title":"Home Assistant","reason":"Enter the token.","owner":"home","fields":[{"id":"token","label":"Access token","kind":"secret","required":true,"placeholder":"","options":[]}]}}"#
        let oauthJSON = #"{"status":"input","text":"Open this authorization link:\nhttps://auth.example/authorize?state=s1\n","route":"local","tool":null,"input":{"request_id":"req-oauth","title":"Authorize MCP sentry","reason":"Open this authorization link:\nhttps://auth.example/authorize?state=s1\n","owner":"mcp","open_url":"https://auth.example/authorize?state=s1","fields":[{"id":"redirect","label":"Redirect URL (optional)","kind":"url","required":false,"placeholder":"https://…?code=…","options":[]}]}}"#
        let transport = ScriptedTransport(responses: [
            raw(200, #"{"token":"sess-1"}"#),
            raw(200, #"{"threads":["home"]}"#),
            raw(200, #"{"enabled":false}"#),
            raw(200, inputJSON),
            raw(200, #"{"threads":["home"]}"#),
            raw(200, #"{"status":"reply","text":"connected","route":"local","tool":null}"#),
            raw(200, #"{"threads":["home"]}"#),
            raw(200, inputJSON),
            raw(200, #"{"threads":["home"]}"#),
            raw(200, #"{"status":"reply","text":"cancelled","route":"local","tool":null}"#),
            raw(200, #"{"threads":["home"]}"#),
            raw(200, oauthJSON),
            raw(200, #"{"threads":["home"]}"#),
        ])
        let shell = Shell(transport: transport)
        try await shell.signIn(instance: URL(string: "http://127.0.0.1:8787")!, accountID: "ada", password: "pw")
        try await shell.send(conversationID: "home", text: "connect ha")
        guard case .input(let threads, let prompt, let request) = await shell.phase else {
            Issue.record("expected input phase")
            return
        }
        #expect(threads == ["home"])
        #expect(prompt == "Enter the token.")
        #expect(request.requestID == "req-1")
        #expect(request.fields.first?.kind == "secret")
        try await shell.submitInput(conversationID: "home", requestID: "req-1", values: ["token": "secret-token"])
        #expect(await shell.phase == .ready(threads: ["home"], reply: "connected"))
        let calls = await transport.calls
        let submit = String(decoding: calls[5].body ?? Data(), as: UTF8.self)
        #expect(submit.contains("\"request_id\":\"req-1\""))
        #expect(submit.contains("secret-token"))
        try await shell.send(conversationID: "home", text: "again")
        try await shell.cancelInput(conversationID: "home", requestID: "req-1")
        #expect(await shell.phase == .ready(threads: ["home"], reply: "cancelled"))
        let cancelCall = await transport.calls.reversed().first(where: { $0.body != nil })
        let cancelBody = String(decoding: cancelCall?.body ?? Data(), as: UTF8.self)
        #expect(cancelBody.contains("\"cancel\":true"))
        try await shell.send(conversationID: "home", text: "sentry oauth")
        guard case .input(_, let oauthPrompt, let oauthRequest) = await shell.phase else {
            Issue.record("expected oauth input")
            return
        }
        #expect(oauthPrompt.contains("https://auth.example/authorize"))
        #expect(oauthRequest.openURL == "https://auth.example/authorize?state=s1")
        #expect(oauthRequest.fields.first?.fieldID == "redirect")
    }

    @Test func connectingAMailboxDoesNotKeepThePassword() async throws {
        let transport = ScriptedTransport(responses: [
            raw(200, #"{"token":"sess-1"}"#),
            raw(200, #"{"threads":[]}"#),
            raw(200, #"{"enabled":false}"#),
            raw(200, #"{"name":"mailbox","connected":true}"#),
            raw(200, #"{"name":"calendar","connected":true}"#),
            raw(200, #"{"enabled":true}"#),
            raw(200, #"{"status":"reply","text":"ok","route":"local","tool":null}"#),
            raw(200, #"{"threads":[]}"#),
        ])
        let shell = Shell(transport: transport)
        await #expect(throws: RobinFailure(status: 401, message: "login required")) {
            try await shell.connectMailbox(imapHost: "imap.example", smtpHost: "smtp.example", user: "ada@example.com", password: "mailbox-password-ada")
        }
        #expect(await transport.calls.isEmpty)
        try await shell.signIn(instance: URL(string: "http://127.0.0.1:8787")!, accountID: "ada", password: "pw")
        try await shell.connectMailbox(imapHost: "imap.example", smtpHost: "smtp.example", user: "ada@example.com", password: "mailbox-password-ada")
        try await shell.connectCalendar(url: "https://cal.example/ada", user: "ada@example.com", password: "calendar-password-ada")
        try await shell.setSchedule(enabled: true)
        #expect(await shell.scheduleEnabled == true)
        try await shell.send(conversationID: "home", text: "hello")
        let calls = await transport.calls
        let mailbox = String(decoding: calls[3].body ?? Data(), as: UTF8.self)
        #expect(calls[3].url.absoluteString == "http://127.0.0.1:8787/v1/secrets")
        #expect(mailbox.contains("\"name\":\"mailbox\""))
        #expect(mailbox.contains("mailbox-password-ada"))
        #expect(mailbox.contains("imap.example"))
        let calendar = String(decoding: calls[4].body ?? Data(), as: UTF8.self)
        #expect(calendar.contains("\"name\":\"calendar\""))
        #expect(calendar.contains("cal.example"))
        let schedule = String(decoding: calls[5].body ?? Data(), as: UTF8.self)
        #expect(schedule.contains("\"enabled\":true"))
        let sent = String(decoding: calls[6].body ?? Data(), as: UTF8.self)
        #expect(!sent.contains("mailbox-password-ada"))
        #expect(!sent.contains("calendar-password-ada"))
        #expect(!sent.contains("pw"))
    }

    @Test func theScreensOnlyImportTheShell() throws {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Sources/RobinApp")
        let files = try FileManager.default.contentsOfDirectory(at: root, includingPropertiesForKeys: nil)
        let source = try files.map { try String(contentsOf: $0, encoding: .utf8) }.joined(separator: "\n")
        #expect(source.contains("import RobinKit"))
        #expect(source.contains("import SwiftUI"))
        #expect(source.contains("SecureField"))
        #expect(source.contains("InputFormView"))
        #expect(source.contains("Open authorization page"))
        #expect(source.contains("submitInput"))
        #expect(source.contains("Needs you"))
        #expect(source.contains("UserNotifications"))
        #expect(source.contains("Connect mail"))
        #expect(source.contains("Connect calendar"))
        #expect(source.contains("Check mail and calendar"))
        #expect(source.contains("Export vault"))
        #expect(source.contains("Import vault"))
        #expect(source.contains("vaultPassphrase = \"\""))
        #expect(source.contains("mailPassword = \"\""))
        #expect(source.contains("calendarPassword = \"\""))
        #expect(!source.contains("chat/completions"))
        #expect(!source.contains("openai"))
        #expect(!source.contains("URLSession"))
    }
}

private func raw(_ status: Int, _ json: String) -> RobinRaw {
    RobinRaw(status: status, data: Data(json.utf8))
}
