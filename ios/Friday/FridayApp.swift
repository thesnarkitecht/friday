import SwiftUI

@main
struct FridayApp: App {
    @StateObject private var friday = Friday.shared

    var body: some Scene {
        WindowGroup {
            ContentView().environmentObject(friday).preferredColorScheme(.dark)
                .task { await friday.start() }
        }
    }
}
