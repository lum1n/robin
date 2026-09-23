import Foundation
import Testing

@testable import RobinKit

struct ShellTests {
    @Test func aConfirmWaitsAndLeavingDropsTheSession() async throws {
        let transport = ScriptedTransport(responses: [
            raw(200, #"{"token":"sess-1"}"#),
            raw(200, #"{"threads":["home"]}"#),
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
        #expect(calls[2].url.absoluteString == "http://127.0.0.1:8787/v1/messages")
        #expect(!String(decoding: calls[2].body ?? Data(), as: UTF8.self).contains("pw"))
        let confirm = String(decoding: calls[4].body ?? Data(), as: UTF8.self)
        #expect(confirm.contains("\"confirm\":true"))

        await shell.leave()
        #expect(await shell.phase == .signedOut)
        await #expect(throws: RobinFailure(status: 401, message: "login required")) {
            try await shell.send(conversationID: "home", text: "again")
        }
        #expect(await transport.calls.count == 6)
    }

    @Test func switchingInstanceUsesTheNewAddress() async throws {
        let transport = ScriptedTransport(responses: [
            raw(200, #"{"token":"house"}"#),
            raw(200, #"{"threads":[]}"#),
            raw(200, #"{"token":"private"}"#),
            raw(200, #"{"threads":["desk"]}"#),
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
        #expect(calls[2].url.host == "robin-ada.exe.xyz")
        #expect(calls[4].url.host == "robin-ada.exe.xyz")
        #expect(calls[4].token == "private")
        #expect(await shell.phase == .ready(threads: ["desk"], reply: "hello"))
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
        #expect(!source.contains("chat/completions"))
        #expect(!source.contains("openai"))
        #expect(!source.contains("URLSession"))
    }
}

private func raw(_ status: Int, _ json: String) -> RobinRaw {
    RobinRaw(status: status, data: Data(json.utf8))
}
