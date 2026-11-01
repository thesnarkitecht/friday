import SwiftUI

struct ContentView: View {
    @EnvironmentObject var friday: Friday
    @EnvironmentObject var glasses: Glasses
    @State private var typed = ""
    @State private var showSettings = false

    var body: some View {
        VStack(spacing: 16) {
            HStack {
                Circle().fill(friday.connected ? .green : .red).frame(width: 8, height: 8)
                    .accessibilityLabel(friday.connected ? "Connected to home server" : "Not connected")
                Text("FRIDAY").font(.system(.headline, design: .monospaced)).tracking(4)
                Spacer()
                Button { showSettings = true } label: { Image(systemName: "gearshape") }
                    .accessibilityLabel("Settings")
            }
            Orb(active: friday.listening).frame(height: 160)
                .onTapGesture { friday.listening ? friday.stopListening() : friday.startListening() }
                .accessibilityLabel(friday.listening ? "Listening. Tap to mute." : "Muted. Tap to listen.")
                .accessibilityAddTraits(.isButton)
            if let lens = glasses.previewView {
                LensPreview(view: lens).frame(height: 120).clipShape(RoundedRectangle(cornerRadius: 12))
                    .accessibilityLabel("Simulated glasses display")
            }
            Text(friday.hearing.isEmpty ? " " : friday.hearing).font(.callout).foregroundStyle(.secondary).lineLimit(2)
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 10) {
                        ForEach(friday.lines) { line in
                            VStack(alignment: .leading, spacing: 2) {
                                Text(line.who).font(.caption2.monospaced()).foregroundStyle(line.who == "FRIDAY" ? .cyan : .secondary)
                                Text(line.text)
                            }.id(line.id)
                        }
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }
                .onChange(of: friday.lines.count) { if let l = friday.lines.last { proxy.scrollTo(l.id) } }
            }
            TextField("Type to FRIDAY", text: $typed).textFieldStyle(.roundedBorder)
                .onSubmit { if !typed.isEmpty { friday.type(typed); typed = "" } }
        }
        .padding()
        .sheet(isPresented: $showSettings) { SettingsView() }
    }
}

struct Orb: View {
    let active: Bool
    @State private var pulse = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        ZStack {
            Circle().stroke(.cyan.opacity(0.25), lineWidth: 1).scaleEffect(pulse ? 1.15 : 0.9)
            Circle().fill(RadialGradient(colors: [.cyan.opacity(active ? 0.9 : 0.3), .clear], center: .center,
                                         startRadius: 4, endRadius: 70))
        }
        .onAppear {
            guard !reduceMotion else { return }
            withAnimation(.easeInOut(duration: 1.6).repeatForever()) { pulse = true }
        }
    }
}

struct LensPreview: UIViewRepresentable {
    let view: UIView
    func makeUIView(context: Context) -> UIView { view }
    func updateUIView(_ uiView: UIView, context: Context) {}
}

struct SettingsView: View {
    @Environment(\.dismiss) private var dismiss
    @EnvironmentObject var glasses: Glasses
    @State private var url = Settings.serverURL
    @State private var token = Settings.token

    var body: some View {
        NavigationStack {
            Form {
                TextField("Home server URL", text: $url).textInputAutocapitalization(.never).keyboardType(.URL)
                SecureField("Token", text: $token)
                Section("Meta glasses") {
                    Text(glasses.status).foregroundStyle(.secondary)
                    Button("Connect glasses (opens Meta AI)") { Task { await glasses.register() } }
                    Button("Simulate Ray-Ban Display (no hardware)") { glasses.simulate() }
                }
            }
            .navigationTitle("Settings")
            .toolbar {
                Button("Save") {
                    Settings.serverURL = url; Settings.token = token
                    Friday.shared.connect(); dismiss()
                }
            }
        }
    }
}
