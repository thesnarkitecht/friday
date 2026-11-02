import Combine
import Foundation
import MWDATCamera
import MWDATCore
import MWDATDisplay
import MWDATMockDevice
import UIKit

// Kept free of SwiftUI on purpose: MWDATDisplay's Text/Button/Image DSL names clash with SwiftUI's.

/// Meta glasses via the Wearables Device Access Toolkit: FRIDAY's replies on the Ray-Ban Display lens,
/// and single camera frames for "what am I looking at?". Audio needs none of this: the glasses are a
/// Bluetooth headset, and the voice loop already routes to Bluetooth.
@MainActor
final class Glasses: ObservableObject {
    static let shared = Glasses()

    @Published var status = "Not connected"
    @Published var previewView: UIView?   // simulated lens, shown in the app when simulating

    private var session: DeviceSession?
    private var display: Display?
    private var displayReady = false
    private let tokens = ListenerTokenBag()

    func configure() {
        do { try Wearables.configure() } catch { status = "Glasses SDK error: \(error)" }
    }

    /// Meta AI calls back into the app after registration / permission prompts.
    func handle(_ url: URL) {
        guard URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?
            .contains(where: { $0.name == "metaWearablesAction" }) == true else { return }
        Task { _ = try? await Wearables.shared.handleUrl(url) }
    }

    func register() async {
        do { try await Wearables.shared.startRegistration() } catch { status = error.localizedDescription }
    }

    /// No glasses? Pair simulated Ray-Ban Display glasses whose camera is the iPhone's back camera.
    func simulate() {
        do {
            MockDeviceKit.shared.enable()
            let glasses = try MockDeviceKit.shared.pairGlasses(model: .metaRayBanDisplay)
            glasses.powerOn()
            glasses.unfold()
            glasses.don()
            glasses.services.camera.setCameraFeed(cameraFacing: .back)
            previewView = glasses.services.display.createPreviewView()
            Task { await connect() }
        } catch {
            status = "Simulator error: \(error.localizedDescription)"
        }
    }

    func connect() async {
        guard session == nil else { return }
        do {
            let selector = AutoDeviceSelector(wearables: Wearables.shared)
            status = "Waiting for glasses…"
            for await device in selector.activeDeviceStream() where device != nil { break }
            let s = try Wearables.shared.createSession(deviceSelector: selector)
            session = s
            let started = Task { for await state in s.stateStream() where state == .started { return } }
            try s.start()
            await started.value
            status = "Glasses connected"

            if let id = selector.activeDevice, Wearables.shared.deviceForIdentifier(id)?.supportsDisplay() == true {
                let d = try s.addDisplay()
                display = d
                d.statePublisher.listen { [weak self] state in
                    Task { @MainActor in self?.displayReady = state == .started }
                }.store(in: tokens)
                d.start()
            }
        } catch DeviceSessionError.datAppOnTheGlassesUpdateRequired {
            status = "Update the glasses app in Meta AI"
            try? await Wearables.shared.openDATGlassesAppUpdate()
        } catch {
            session = nil
            status = error.localizedDescription
        }
    }

    /// Show FRIDAY's latest line on the lens (Ray-Ban Display only; silently skipped otherwise).
    func show(_ text: String) async {
        guard displayReady, let display else { return }
        try? await display.send(
            FlexBox(direction: .column, spacing: 8) {
                Text("FRIDAY", style: .heading)
                Text(text, style: .body)
            }
            .padding(24)
            .background(.card)
        )
    }

    /// One JPEG from the glasses camera, or nil within ~10s. The camera runs only for this capture.
    func frame() async -> Data? {
        guard let session else { return nil }
        do {
            if try await Wearables.shared.checkPermissionStatus(.camera) != .granted,
               try await Wearables.shared.requestPermission(.camera) != .granted { return nil }
            guard let camera = try session.addCamera(
                config: StreamConfiguration(videoCodec: .raw, resolution: .medium, frameRate: 15)) else { return nil }
            let bag = ListenerTokenBag()
            defer { bag.clear(); camera.stop() }

            let (frames, feed) = AsyncStream.makeStream(of: Data.self, bufferingPolicy: .bufferingNewest(1))
            camera.stream.videoFramePublisher.listen { frame in
                if let jpg = frame.makeUIImage()?.jpegData(compressionQuality: 0.7) { feed.yield(jpg) }
            }.store(in: bag)
            camera.stream.start()
            let timeout = Task { try? await Task.sleep(for: .seconds(10)); feed.finish() }
            defer { timeout.cancel() }

            var seen = 0
            for await jpg in frames {
                seen += 1
                if seen >= 5 { return jpg }  // first frames are dark while exposure settles
            }
        } catch {
            status = error.localizedDescription
        }
        return nil
    }
}
