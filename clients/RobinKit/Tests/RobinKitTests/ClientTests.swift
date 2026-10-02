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
    @Test func requestAddsThisAccountAndRefusesOtherPaths() async throws {
        let transport = ScriptedTransport(responses: [
            RobinRaw(status: 200, data: Data(#"{"token":"sess-1"}"#.utf8)),
            RobinRaw(status: 200, data: Data(#"{"name":"oda"}"#.utf8)),
            RobinRaw(status: 200, data: Data(#"{"servers":[]}"#.utf8)),
            RobinRaw(status: 400, data: Data(#"{"error":"only household admins"}"#.utf8)),
        ])
        let client = RobinClient(baseURL: URL(string: "http://127.0.0.1:8787")!, accountID: "ada", transport: transport)
        try await client.login(password: "correct-horse")
        let added = try await client.request(
            method: "POST",
            path: "/v1/mcp/add",
            json: Data(#"{"docs_url":"https://github.com/o/r","account_id":"bea"}"#.utf8)
        )
        #expect(String(decoding: added, as: UTF8.self).contains("oda"))
        _ = try await client.request(method: "GET", path: "/v1/mcp")
        await #expect(throws: RobinFailure.self) {
            try await client.request(method: "POST", path: "/v1/mcp/add", json: nil)
        }
        await #expect(throws: RobinFailure.self) {
            try await client.request(method: "GET", path: "/admin")
        }
        let calls = await transport.calls
        let sent = String(decoding: calls[1].body ?? Data(), as: UTF8.self)
        #expect(sent.contains("\"account_id\":\"ada\"") && sent.contains("docs_url"))
        #expect(calls[1].token == "sess-1")
        #expect(calls[2].url.absoluteString == "http://127.0.0.1:8787/v1/mcp?account_id=ada")
        #expect(calls.count == 4)
    }

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

    @Test func sendCarriesDeviceContextWithACoarseFix() async throws {
        let transport = ScriptedTransport(responses: [
            RobinRaw(status: 200, data: Data(#"{"token":"sess-1"}"#.utf8)),
            RobinRaw(status: 200, data: Data(#"{"status":"reply","text":"ok","route":"cloud","tool":null}"#.utf8)),
            RobinRaw(status: 200, data: Data(#"{"status":"reply","text":"ok","route":"cloud","tool":null}"#.utf8)),
        ])
        let client = RobinClient(baseURL: URL(string: "http://127.0.0.1:8787")!, accountID: "ada", transport: transport)
        try await client.login(password: "pw")
        let context = DeviceContext(
            timezone: "Europe/Oslo",
            locale: "nb_NO",
            device: "iphone",
            units: "metric",
            currency: "NOK",
            location: DeviceLocation(
                locality: "Oslo",
                country: "Norway",
                latitude: 59.913868,
                longitude: 10.752245,
                countryCode: "NO"
            )
        )
        _ = try await client.send(conversationID: "home", text: "football today", context: context)
        _ = try await client.send(conversationID: "home", text: "no context")

        let calls = await transport.calls
        let body = try #require(JSONSerialization.jsonObject(with: calls[1].body ?? Data()) as? [String: Any])
        let sent = try #require(body["context"] as? [String: Any])
        #expect(sent["timezone"] as? String == "Europe/Oslo")
        #expect(sent["device"] as? String == "iphone")
        let location = try #require(sent["location"] as? [String: Any])
        #expect(location["locality"] as? String == "Oslo")
        #expect(location["latitude"] as? Double == 59.91)
        #expect(location["longitude"] as? Double == 10.75)
        #expect(location["region"] == nil)
        #expect(location["country_code"] as? String == "NO")
        #expect(sent["currency"] as? String == "NOK")
        let bare = String(decoding: calls[2].body ?? Data(), as: UTF8.self)
        #expect(!bare.contains("\"context\""))
        #expect(DeviceContext.current().units == "metric" || DeviceContext.current().units == "imperial")
    }

    @Test func householdAndPreferencesRoundTripOnThisAccount() async throws {
        let transport = ScriptedTransport(responses: [
            RobinRaw(status: 200, data: Data(#"{"token":"sess-1"}"#.utf8)),
            RobinRaw(status: 200, data: Data(#"{"members":[{"relation":"child","given_name":"Ola","family_name":"","birth_year":2016}]}"#.utf8)),
            RobinRaw(status: 200, data: Data(#"{"members":[{"relation":"partner","given_name":"Kari","family_name":"Nordmann","birth_year":null}]}"#.utf8)),
            RobinRaw(status: 200, data: Data(#"{"style":"concise","sources":[{"topic":"football","host":"nrk.no"}]}"#.utf8)),
        ])
        let client = RobinClient(baseURL: URL(string: "http://127.0.0.1:8787")!, accountID: "ada", transport: transport)
        try await client.login(password: "pw")
        let loaded = try await client.household()
        #expect(loaded.first?.givenName == "Ola" && loaded.first?.birthYear == 2016)
        let saved = try await client.saveHousehold([HouseholdMember(relation: "partner", givenName: "Kari", familyName: "Nordmann")])
        #expect(saved.first?.familyName == "Nordmann" && saved.first?.birthYear == nil)
        let preferences = try await client.savePreferences(
            Preferences(style: "concise", sources: [PreferredSource(topic: "football", host: "nrk.no")])
        )
        #expect(preferences.sources.first?.host == "nrk.no")

        let calls = await transport.calls
        #expect(calls[1].url.absoluteString == "http://127.0.0.1:8787/v1/household?account_id=ada")
        let household = try #require(JSONSerialization.jsonObject(with: calls[2].body ?? Data()) as? [String: Any])
        #expect(household["account_id"] as? String == "ada")
        let member = try #require((household["members"] as? [[String: Any]])?.first)
        #expect(member["given_name"] as? String == "Kari")
        #expect(member["family_name"] as? String == "Nordmann")
        #expect(member["birth_year"] == nil && member["id"] == nil)
        let body = try #require(JSONSerialization.jsonObject(with: calls[3].body ?? Data()) as? [String: Any])
        let sent = try #require(body["preferences"] as? [String: Any])
        #expect(sent["style"] as? String == "concise")
        let source = try #require((sent["sources"] as? [[String: Any]])?.first)
        #expect(source["topic"] as? String == "football" && source["id"] == nil)
        #expect(calls[3].token == "sess-1")
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

    @Test func overviewDecodesStatusWithoutSecrets() async throws {
        let body = #"{"account_id":"ada","schedule_enabled":false,"connected":["calendar"],"profile_present":[],"mcp":[],"capabilities":[{"id":"agenda","status":"calendar: connected","tools":["calendar_list"]}],"connectors":["calendar: connected"],"threads":[],"private":{"https_url":"https://robin-ada.example","ready":true}}"#
        let transport = ScriptedTransport(responses: [
            RobinRaw(status: 200, data: Data(#"{"token":"sess-1"}"#.utf8)),
            RobinRaw(status: 200, data: Data(body.utf8)),
        ])
        let client = RobinClient(
            baseURL: URL(string: "http://127.0.0.1:8787")!,
            accountID: "ada",
            transport: transport
        )
        try await client.login(password: "correct-horse")
        let overview = try await client.overview()
        #expect(overview.connected == ["calendar"])
        #expect(overview.privateInstance?.httpsURL == "https://robin-ada.example")
        #expect(overview.privateInstance?.ready == true)
        let calls = await transport.calls
        #expect(calls[1].url.absoluteString == "http://127.0.0.1:8787/v1/status?account_id=ada")
        #expect(calls[1].method == "GET")
        #expect(calls[1].token == "sess-1")
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

    @Test func latestRobinReplyFindsAnswerAfterMatchingUserTurn() {
        let turns = [
            Turn(role: "user", text: "earlier"),
            Turn(role: "reply", text: "old"),
            Turn(role: "user", text: "find cars"),
            Turn(role: "reply", text: "Found GLC listings."),
        ]
        let reply = latestRobinReply(in: turns, afterUserText: "find cars")
        #expect(reply?.text == "Found GLC listings.")
        #expect(latestRobinReply(in: turns, afterUserText: "missing") == nil)
    }
}
