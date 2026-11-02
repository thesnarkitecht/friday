import AppIntents
import Foundation

/// "Hey Siri, ask Friday …", the Action button, or any Shortcut can reach FRIDAY without opening the app.
struct AskFridayIntent: AppIntent {
    static var title: LocalizedStringResource = "Ask FRIDAY"
    @Parameter(title: "Request") var request: String

    func perform() async throws -> some IntentResult & ReturnsValue<String> & ProvidesDialog {
        guard let url = URL(string: Settings.serverURL + "/api/ask") else { throw URLError(.badURL) }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("Bearer \(Settings.token)", forHTTPHeaderField: "Authorization")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: ["text": request])
        let (data, _) = try await URLSession.shared.data(for: req)
        let reply = (try JSONSerialization.jsonObject(with: data) as? [String: Any])?["reply"] as? String
            ?? "I couldn't reach the home server."
        return .result(value: reply, dialog: IntentDialog(stringLiteral: reply))
    }
}

struct FridayShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(intent: AskFridayIntent(), phrases: ["Ask \(.applicationName)", "Talk to \(.applicationName)"],
                    shortTitle: "Ask FRIDAY", systemImageName: "waveform")
    }
}
