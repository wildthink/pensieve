// The Ed25519 key that signs releases. The app only installs an update whose Pensieve.dmg.sig verifies against
// Updater.publicKey, so a release swapped on GitHub (or anywhere on the way) can't install itself.
//   swift mac/scripts/update_key.swift keygen <key file>    new key (refuses to overwrite); prints the public key
//   swift mac/scripts/update_key.swift public <key file>    the public key for Updater.publicKey
//   swift mac/scripts/update_key.swift sign <key file> <dmg> the signature (base64) on stdout
// Keep the key file out of the repo and backed up (a password manager): without it no installed app accepts an update.
import CryptoKit
import Foundation

func fail(_ msg: String) -> Never {
    FileHandle.standardError.write(Data((msg + "\n").utf8))
    exit(1)
}

func load(_ path: String) -> Curve25519.Signing.PrivateKey {
    guard let s = try? String(contentsOfFile: path, encoding: .utf8),
          let raw = Data(base64Encoded: s.trimmingCharacters(in: .whitespacesAndNewlines)),
          let key = try? Curve25519.Signing.PrivateKey(rawRepresentation: raw) else { fail("can't read a key from \(path)") }
    return key
}

let args = CommandLine.arguments.dropFirst()
switch (args.first, args.count) {
case ("keygen", 2):
    let path = args[args.startIndex + 1]
    if FileManager.default.fileExists(atPath: path) { fail("\(path) exists; not overwriting it") }
    let key = Curve25519.Signing.PrivateKey()
    let dir = (path as NSString).deletingLastPathComponent
    if !dir.isEmpty {
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
    }
    guard FileManager.default.createFile(atPath: path, contents: Data(key.rawRepresentation.base64EncodedString().utf8),
                                         attributes: [.posixPermissions: 0o600]) else { fail("can't write \(path)") }
    print(key.publicKey.rawRepresentation.base64EncodedString())
case ("public", 2):
    print(load(args[args.startIndex + 1]).publicKey.rawRepresentation.base64EncodedString())
case ("sign", 3):
    let key = load(args[args.startIndex + 1])
    let file = args[args.startIndex + 2]
    guard let data = try? Data(contentsOf: URL(fileURLWithPath: file), options: .mappedIfSafe) else { fail("can't read \(file)") }
    guard let sig = try? key.signature(for: data) else { fail("signing failed") }
    print(sig.base64EncodedString())
default:
    fail("usage: update_key.swift keygen <key> | public <key> | sign <key> <file>")
}
