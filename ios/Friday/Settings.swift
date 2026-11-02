import Foundation
import Security

/// Server address lives in UserDefaults; the bearer token lives in the Keychain.
enum Settings {
    static var serverURL: String {
        get { UserDefaults.standard.string(forKey: "serverURL") ?? "http://friday-box.tailnet.ts.net:8080" }
        set { UserDefaults.standard.set(newValue, forKey: "serverURL") }
    }

    static var token: String {
        get {
            var item: CFTypeRef?
            let q: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrAccount as String: "friday-token",
                                    kSecReturnData as String: true]
            guard SecItemCopyMatching(q as CFDictionary, &item) == errSecSuccess, let d = item as? Data else { return "" }
            return String(decoding: d, as: UTF8.self)
        }
        set {
            let q: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrAccount as String: "friday-token"]
            SecItemDelete(q as CFDictionary)
            var add = q
            add[kSecValueData as String] = Data(newValue.utf8)
            add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
            SecItemAdd(add as CFDictionary, nil)
        }
    }
}
