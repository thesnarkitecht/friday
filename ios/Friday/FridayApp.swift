import SwiftUI

@main
struct FridayApp: App {
    @StateObject private var friday = Friday.shared
    @StateObject private var glasses = Glasses.shared

    init() { Glasses.shared.configure() }

    var body: some Scene {
        WindowGroup {
            ContentView().environmentObject(friday).environmentObject(glasses).preferredColorScheme(.dark)
                .task { await friday.start() }
                .onOpenURL { Glasses.shared.handle($0) }
        }
    }
}
