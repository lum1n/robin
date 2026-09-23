import RobinKit
import SwiftUI

@MainActor
final class ShellModel: ObservableObject {
    @Published private(set) var phase: ShellPhase = .signedOut
    @Published var instance = "http://127.0.0.1:8787"
    @Published var accountID = ""
    @Published var password = ""
    @Published var conversationID = "home"
    @Published var draft = ""
    @Published var failure: String?

    private let shell: Shell

    init(shell: Shell = Shell()) {
        self.shell = shell
    }

    func signIn() async {
        failure = nil
        guard let url = URL(string: instance), !accountID.isEmpty, !password.isEmpty else {
            failure = "Instance, account, and password are required."
            return
        }
        let secret = password
        password = ""
        do {
            try await shell.signIn(instance: url, accountID: accountID, password: secret)
            phase = await shell.phase
        } catch let error as RobinFailure {
            failure = error.message
        } catch {
            failure = "Could not reach Robin."
        }
    }

    func send() async {
        let text = draft
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

    func leave() async {
        await shell.leave()
        draft = ""
        failure = nil
        phase = .signedOut
    }

    private func perform(_ work: () async throws -> Void) async {
        failure = nil
        do {
            try await work()
            phase = await shell.phase
        } catch let error as RobinFailure {
            failure = error.message
        } catch {
            failure = "Could not reach Robin."
        }
    }
}

struct RobinRootView: View {
    @StateObject private var model = ShellModel()

    var body: some View {
        NavigationStack {
            switch model.phase {
            case .signedOut:
                SignInForm(model: model)
            case .ready, .confirm:
                ConversationForm(model: model)
            }
        }
    }
}

private struct SignInForm: View {
    @ObservedObject var model: ShellModel

    var body: some View {
        Form {
            TextField("Instance", text: $model.instance)
                .robinField()
            TextField("Account", text: $model.accountID)
                .robinField()
            SecureField("Password", text: $model.password)
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
        Form {
            switch model.phase {
            case .ready(let threads, let reply):
                Text(threads.isEmpty ? "No threads yet" : threads.joined(separator: ", "))
                if let reply {
                    Text(reply)
                }
            case .confirm(_, let prompt, let tool):
                Text(prompt)
                Text(tool)
                Button("Confirm") {
                    Task { await model.confirm() }
                }
            case .signedOut:
                EmptyView()
            }
            TextField("Thread", text: $model.conversationID)
                .robinField()
            TextField("Message", text: $model.draft)
            Button("Send") {
                Task { await model.send() }
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

private extension View {
    func robinField() -> some View {
        #if os(iOS)
        self.textInputAutocapitalization(.never).autocorrectionDisabled()
        #else
        self.autocorrectionDisabled()
        #endif
    }
}

@main
struct RobinApp: App {
    var body: some Scene {
        WindowGroup {
            RobinRootView()
        }
    }
}
