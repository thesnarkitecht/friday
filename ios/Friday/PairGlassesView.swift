import CoreImage.CIFilterBuiltins
import SwiftUI

/// Shows a one-time QR code. Scanning it on Meta Ray-Ban Display adds the FRIDAY glasses web app,
/// which trades the code for access and joins the same live conversation as this phone.
struct PairGlassesView: View {
    @State private var url: String?
    @State private var error: String?

    var body: some View {
        VStack(spacing: 16) {
            Text("Pair your glasses").font(.title2.bold())
            Text("Scan this code with your Meta Ray-Ban Display to add FRIDAY. It works once and expires in 10 minutes.")
                .font(.callout).foregroundStyle(.secondary).multilineTextAlignment(.center)
            if let url, let image = qr(url) {
                Image(uiImage: image).interpolation(.none).resizable().scaledToFit()
                    .frame(maxWidth: 260).padding(12).background(.white, in: RoundedRectangle(cornerRadius: 16))
                    .accessibilityLabel("Pairing QR code")
                Button("New code") { Task { await load() } }
            } else if let error {
                Text(error).foregroundStyle(.red).multilineTextAlignment(.center)
                Button("Try again") { Task { await load() } }
            } else {
                ProgressView()
            }
        }
        .padding(24)
        .task { await load() }
    }

    private func load() async {
        url = nil; error = nil
        guard let endpoint = URL(string: Settings.serverURL + "/api/pair") else { error = "Check the server URL in Settings."; return }
        var req = URLRequest(url: endpoint)
        req.httpMethod = "POST"
        req.setValue("Bearer \(Settings.token)", forHTTPHeaderField: "Authorization")
        do {
            let (data, response) = try await URLSession.shared.data(for: req)
            guard (response as? HTTPURLResponse)?.statusCode == 200 else { error = "The server refused. Check your token in Settings."; return }
            url = (try JSONSerialization.jsonObject(with: data) as? [String: Any])?["url"] as? String
        } catch {
            self.error = "Can't reach your FRIDAY server. Is Tailscale connected?"
        }
    }

    private func qr(_ text: String) -> UIImage? {
        let filter = CIFilter.qrCodeGenerator()
        filter.message = Data(text.utf8)
        filter.correctionLevel = "M"
        guard let out = filter.outputImage?.transformed(by: CGAffineTransform(scaleX: 8, y: 8)),
              let cg = CIContext().createCGImage(out, from: out.extent) else { return nil }
        return UIImage(cgImage: cg)
    }
}
