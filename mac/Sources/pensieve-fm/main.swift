// pensieve-fm: Apple's models (the Foundation Models framework) for the backend's AI engines (pensieve/llm.py):
// `apple`, the on-device model, and `pcc`, Apple's larger model on Private Cloud Compute. It ships inside
// Pensieve.app, next to the app's own binary.
//   pensieve-fm status [pcc]   {"available": true, "context": <tokens>} or {"available": false, "reason": "..."}
//   pensieve-fm respond        reads {"model": "device"|"pcc", "system", "prompt", "max_tokens", "temperature",
//                              "schema"?} on stdin and writes {"text": "..."}; with a JSON schema the reply is guided
//                              to it (always valid JSON)
// Exit codes: 0 done, 2 the model isn't available now (older macOS, Apple Intelligence off, model downloading; for
// PCC also no entitlement, network down, quota used up), 3 the prompt doesn't fit the context window, 4 Apple's
// guardrails refused it, 1 anything else.
// The model gets no tools: it reads the prompt and writes text, nothing more.
import Foundation
import Security
#if canImport(FoundationModels)
import FoundationModels
#endif

func write(_ o: [String: Any]) {
    FileHandle.standardOutput.write(try! JSONSerialization.data(withJSONObject: o))
    FileHandle.standardOutput.write(Data("\n".utf8))
}

func fail(_ code: Int32, _ msg: String) -> Never {
    FileHandle.standardError.write(Data((msg + "\n").utf8))
    exit(code)
}

/// Private Cloud Compute needs this managed entitlement, which Apple grants on request to a developer account.
/// Without it the framework stops the process (a fatal error, not a thrown one), so it's checked before any use.
let pccEntitlement = "com.apple.developer.private-cloud-compute"

func entitled(_ name: String) -> Bool {
    guard let task = SecTaskCreateFromSelf(nil) else { return false }
    return SecTaskCopyValueForEntitlement(task, name as CFString, nil) as? Bool == true
}

#if canImport(FoundationModels)
@available(macOS 26, *)
func deviceModel() -> SystemLanguageModel {
    // Summaries and topic names transform the user's own text, which can be anything (code, logs, arguments in a
    // transcript); the default guardrails refuse too much of it.
    SystemLanguageModel(guardrails: .permissiveContentTransformations)
}

@available(macOS 26, *)
func deviceStatus() -> [String: Any] {
    let m = deviceModel()
    switch m.availability {
    case .available: return ["available": true, "context": m.contextSize]
    case .unavailable(.deviceNotEligible): return off("This Mac can't run Apple Intelligence.")
    case .unavailable(.appleIntelligenceNotEnabled): return off("Turn on Apple Intelligence in System Settings.")
    case .unavailable(.modelNotReady): return off("macOS is still downloading the Apple Intelligence model.")
    case .unavailable(let other): return off("Apple Intelligence isn't available (\(other)).")
    }
}

func off(_ reason: String) -> [String: Any] { ["available": false, "reason": reason] }

func pccStatus() async -> [String: Any] {
    guard #available(macOS 27, *) else { return off("Private Cloud Compute needs macOS 27 or later.") }
    guard entitled(pccEntitlement) else {
        return off("This build of Pensieve isn't signed with Apple's Private Cloud Compute entitlement.")
    }
    let m = PrivateCloudComputeLanguageModel()
    switch m.availability {
    case .available: break
    case .unavailable(.deviceNotEligible): return off("This Mac can't use Private Cloud Compute.")
    case .unavailable(.systemNotReady): return off("Private Cloud Compute isn't ready yet; check Apple Intelligence in System Settings.")
    case .unavailable(let other): return off("Private Cloud Compute isn't available (\(other)).")
    }
    let quota = m.quotaUsage
    if quota.isLimitReached {
        let when = quota.resetDate.map { " until \($0.formatted(date: .abbreviated, time: .shortened))" } ?? ""
        return off("Pensieve has used its Private Cloud Compute quota\(when).")
    }
    var st: [String: Any] = ["available": true]
    if let n = try? await m.contextSize { st["context"] = n }
    if case .belowLimit(let b) = quota.status, b.isApproachingLimit { st["approaching_limit"] = true }
    return st
}

/// A JSON schema (the subset Pensieve's prompts use: objects, arrays with min/max items, strings, numbers,
/// booleans, string enums) as a schema the model's output is guided by. Properties keep the `required` order,
/// since the model writes them in order (a summary before its tags).
@available(macOS 26, *)
func dynamic(_ s: [String: Any], name: String) -> DynamicGenerationSchema {
    switch s["type"] as? String {
    case "object":
        let props = s["properties"] as? [String: Any] ?? [:]
        let required = s["required"] as? [String] ?? []
        let keys = required.filter { props[$0] != nil } + props.keys.filter { !required.contains($0) }.sorted()
        return DynamicGenerationSchema(name: name, properties: keys.map { k in
            .init(name: k, schema: dynamic(props[k] as? [String: Any] ?? [:], name: "\(name)_\(k)"),
                  isOptional: !required.contains(k))
        })
    case "array":
        return DynamicGenerationSchema(arrayOf: dynamic(s["items"] as? [String: Any] ?? [:], name: "\(name)_item"),
                                       minimumElements: s["minItems"] as? Int, maximumElements: s["maxItems"] as? Int)
    case "integer": return DynamicGenerationSchema(type: Int.self)
    case "number": return DynamicGenerationSchema(type: Double.self)
    case "boolean": return DynamicGenerationSchema(type: Bool.self)
    default:
        if let choices = s["enum"] as? [String] { return DynamicGenerationSchema(name: name, anyOf: choices) }
        return DynamicGenerationSchema(type: String.self)
    }
}

@available(macOS 26, *)
func respond(_ req: [String: Any]) async -> Never {
    let system = (req["system"] as? String).flatMap { $0.isEmpty ? nil : $0 }
    let session: LanguageModelSession
    if req["model"] as? String == "pcc" {
        let st = await pccStatus()
        guard st["available"] as? Bool == true else { fail(2, st["reason"] as? String ?? "Private Cloud Compute isn't available.") }
        guard #available(macOS 27, *) else { fail(2, "Private Cloud Compute needs macOS 27 or later.") }
        session = LanguageModelSession(model: PrivateCloudComputeLanguageModel(), instructions: system)
    } else {
        let st = deviceStatus()
        guard st["available"] as? Bool == true else { fail(2, st["reason"] as? String ?? "Apple Intelligence isn't available.") }
        session = LanguageModelSession(model: deviceModel(), instructions: system)
    }
    let prompt = req["prompt"] as? String ?? ""
    let options = GenerationOptions(temperature: req["temperature"] as? Double,
                                    maximumResponseTokens: req["max_tokens"] as? Int)
    do {
        if let schema = req["schema"] as? [String: Any] {
            let g = try GenerationSchema(root: dynamic(schema, name: "Reply"), dependencies: [])
            write(["text": try await session.respond(to: prompt, schema: g, options: options).content.jsonString])
        } else {
            write(["text": try await session.respond(to: prompt, options: options).content])
        }
        exit(0)
    } catch {
        let (code, msg) = classify(error)
        fail(code, msg)
    }
}

/// An error's exit code. macOS 27 added LanguageModelError and SystemLanguageModel.Error, but still throws
/// LanguageModelSession.GenerationError (macOS 26's) for some failures, such as a prompt over the context window.
@available(macOS 26, *)
func classify(_ error: Error) -> (Int32, String) {
    let tooLong: (Int32, String) = (3, "The prompt is too long for the model.")
    let declined: (Int32, String) = (4, "Apple Intelligence declined this text.")
    let missing: (Int32, String) = (2, "The Apple Intelligence model isn't available right now.")
    if #available(macOS 27, *) {
        if let e = error as? PrivateCloudComputeLanguageModel.Error {
            switch e {  // all temporary: the engine is skipped until it comes back
            case .networkFailure: return (2, "Can't reach Private Cloud Compute (network).")
            case .quotaLimitReached: return (2, "Pensieve has used its Private Cloud Compute quota for now.")
            case .serviceUnavailable: return (2, "Private Cloud Compute is unavailable right now.")
            @unknown default: return (2, "\(e)")
            }
        }
        if let e = error as? LanguageModelError {
            switch e {
            case .contextSizeExceeded: return tooLong
            case .guardrailViolation, .refusal: return declined
            default: return (1, "\(e)")
            }
        }
        if error is SystemLanguageModel.Error { return missing }
    }
    if let e = error as? LanguageModelSession.GenerationError {
        switch e {
        case .exceededContextWindowSize: return tooLong
        case .guardrailViolation, .refusal: return declined
        case .assetsUnavailable: return missing
        default: return (1, "\(e)")
        }
    }
    return (1, "\(error)")
}
#endif

let args = Array(CommandLine.arguments.dropFirst())
#if canImport(FoundationModels)
if #available(macOS 26, *) {
    switch args.first {
    case "status":
        write(args.dropFirst().first == "pcc" ? await pccStatus() : deviceStatus())
        exit(0)
    case "respond":
        let input = FileHandle.standardInput.readDataToEndOfFile()
        guard let req = (try? JSONSerialization.jsonObject(with: input)) as? [String: Any] else { fail(1, "expected a JSON request on stdin") }
        await respond(req)
    default:
        fail(1, "usage: pensieve-fm status [pcc] | respond < request.json")
    }
}
#endif
if args.first == "status" {
    write(["available": false, "reason": "Apple Intelligence needs macOS 26 or later."])
    exit(0)
}
fail(2, "Apple Intelligence needs macOS 26 or later.")
