import AVFoundation
import CoreLocation
import Speech
import SwiftUI
import UserNotifications

struct Line: Identifiable { let id = UUID(); let who: String; let text: String }

/// The whole client: ears (on-device speech recognition), voice, location, and the link to the home box.
/// The server decides what was meant for FRIDAY; the phone just hears, speaks and carries out actions.
@MainActor
final class Friday: NSObject, ObservableObject {
    static let shared = Friday()

    @Published var lines: [Line] = []
    @Published var hearing = ""
    @Published var connected = false
    @Published var listening = false

    private let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
    private let engine = AVAudioEngine()
    private let synth = AVSpeechSynthesizer()
    private let locations = CLLocationManager()
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private var silence: Timer?
    private var socket: URLSessionWebSocketTask?

    func start() async {
        synth.delegate = self
        locations.delegate = self
        _ = await withCheckedContinuation { c in SFSpeechRecognizer.requestAuthorization { c.resume(returning: $0) } }
        _ = await AVAudioApplication.requestRecordPermission()
        _ = try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound])
        locations.requestAlwaysAuthorization()
        locations.startMonitoringSignificantLocationChanges()
        connect()
        startListening()
        Task { await Glasses.shared.connect() }  // no-op wait if no glasses are registered
    }

    // MARK: link to the home box
    func connect() {
        guard var comps = URLComponents(string: Settings.serverURL + "/ws") else { return }
        comps.scheme = comps.scheme == "https" ? "wss" : "ws"
        comps.queryItems = [URLQueryItem(name: "token", value: Settings.token)]
        guard let url = comps.url else { return }
        socket?.cancel()
        socket = URLSession.shared.webSocketTask(with: url)
        socket?.resume()
        connected = true
        receive()
    }

    private func receive() {
        socket?.receive { [weak self] result in
            Task { @MainActor in
                guard let self else { return }
                switch result {
                case .success(.string(let s)):
                    if let msg = try? JSONSerialization.jsonObject(with: Data(s.utf8)) as? [String: Any] { self.handle(msg) }
                    self.receive()
                case .success:
                    self.receive()
                case .failure:
                    self.connected = false
                    try? await Task.sleep(for: .seconds(3))  // home box rebooted or we lost signal; keep trying
                    self.connect()
                }
            }
        }
    }

    private func send(_ msg: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: msg) else { return }
        socket?.send(.string(String(decoding: data, as: UTF8.self))) { _ in }
    }

    private func handle(_ msg: [String: Any]) {
        let text = msg["text"] as? String ?? ""
        switch msg["type"] as? String {
        case "say":
            lines.append(Line(who: "FRIDAY", text: text)); speak(text)
            Task { await Glasses.shared.show(text) }
        case "notify":
            lines.append(Line(who: "FRIDAY", text: text)); speak(text); localNotification(text)
            Task { await Glasses.shared.show(text) }
        case "action" where msg["kind"] as? String == "glasses_frame":
            if let id = msg["id"] as? String { Task { await sendGlassesFrame(id: id) } }
        case "action":
            // Apple-supported hand-offs only: Shortcuts, tel: and sms: all show the owner a system confirmation.
            if let s = msg["url"] as? String, let url = URL(string: s) {
                if UIApplication.shared.applicationState == .active {
                    UIApplication.shared.open(url)
                } else {
                    localNotification("Tap to continue: \(s)")  // iOS only opens URLs from the foreground
                }
            }
        default: break
        }
    }

    private func sendGlassesFrame(id: String) async {
        guard let jpg = await Glasses.shared.frame(), let url = URL(string: Settings.serverURL + "/api/glasses/frame")
        else { return }  // the server times out and tells the model the camera was unavailable
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("Bearer \(Settings.token)", forHTTPHeaderField: "Authorization")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["id": id, "jpeg": jpg.base64EncodedString()])
        _ = try? await URLSession.shared.data(for: req)
    }

    func type(_ text: String) {
        lines.append(Line(who: "You", text: text))
        send(["type": "utterance", "text": "Friday, " + text, "speaker": "owner"])
    }

    // MARK: ears
    func startListening() {
        guard recognizer?.isAvailable == true, !engine.isRunning else { return }
        let session = AVAudioSession.sharedInstance()
        try? session.setCategory(.playAndRecord, mode: .voiceChat, options: [.defaultToSpeaker, .allowBluetooth])
        try? session.setActive(true)
        let input = engine.inputNode
        input.installTap(onBus: 0, bufferSize: 1024, format: input.outputFormat(forBus: 0)) { [weak self] buf, _ in
            self?.request?.append(buf)
        }
        engine.prepare()
        try? engine.start()
        listening = true
        newUtterance()
    }

    func stopListening() {
        engine.stop()
        engine.inputNode.removeTap(onBus: 0)
        task?.cancel()
        listening = false
    }

    /// One recognition task per utterance; a pause of 1.2s ends it and ships the text home.
    private func newUtterance() {
        task?.cancel()
        let req = SFSpeechAudioBufferRecognitionRequest()
        req.requiresOnDeviceRecognition = true  // audio never leaves the phone; only text goes to the home box
        req.shouldReportPartialResults = true
        request = req
        task = recognizer?.recognitionTask(with: req) { [weak self] result, error in
            Task { @MainActor in
                guard let self else { return }
                if let text = result?.bestTranscription.formattedString {
                    self.hearing = text
                    self.silence?.invalidate()
                    self.silence = Timer.scheduledTimer(withTimeInterval: 1.2, repeats: false) { _ in
                        Task { @MainActor in self.finishUtterance() }
                    }
                }
                if error != nil, self.listening { self.newUtterance() }
            }
        }
    }

    private func finishUtterance() {
        let text = hearing.trimmingCharacters(in: .whitespaces)
        hearing = ""
        request?.endAudio()
        if !text.isEmpty {
            if text.lowercased().contains("friday") { lines.append(Line(who: "You", text: text)) }
            send(["type": "utterance", "text": text, "speaker": "unknown"])
        }
        newUtterance()
    }

    // MARK: voice
    private func speak(_ text: String) {
        stopListening()  // don't transcribe ourselves
        let u = AVSpeechUtterance(string: text)
        u.voice = AVSpeechSynthesisVoice(language: "en-GB")
        u.rate = 0.52
        synth.speak(u)
    }

    private func localNotification(_ text: String) {
        guard UIApplication.shared.applicationState != .active else { return }
        let c = UNMutableNotificationContent()
        c.title = "FRIDAY"; c.body = text; c.sound = .default
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: UUID().uuidString, content: c, trigger: nil))
    }
}

extension Friday: AVSpeechSynthesizerDelegate {
    nonisolated func speechSynthesizer(_ s: AVSpeechSynthesizer, didFinish u: AVSpeechUtterance) {
        Task { @MainActor in if !self.synth.isSpeaking { self.startListening() } }
    }
}

extension Friday: CLLocationManagerDelegate {
    nonisolated func locationManager(_ m: CLLocationManager, didUpdateLocations locs: [CLLocation]) {
        guard let l = locs.last else { return }
        Task { @MainActor in
            self.send(["type": "location", "lat": l.coordinate.latitude, "lon": l.coordinate.longitude])
        }
    }
}
