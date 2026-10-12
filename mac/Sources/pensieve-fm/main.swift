// pensieve-fm: Apple's on-device model (the Foundation Models framework) for the backend's `apple` AI engine
// (pensieve/llm.py). It ships inside Pensieve.app, next to the app's own binary.
//   pensieve-fm status    {"available": true} or {"available": false, "reason": "..."}
//   pensieve-fm respond   reads {"system", "prompt", "max_tokens", "temperature", "schema"?} on stdin and writes
//                         {"text": "..."}; with a JSON schema the reply is guided to it (always valid JSON)
// Exit codes: 0 done, 2 the model isn't available (older macOS, Apple Intelligence off, model not downloaded yet),
// 3 the prompt doesn't fit the context window, 4 Apple's guardrails refused it, 1 anything else.
// The model gets no tools: it reads the prompt and writes text, nothing more.
import Foundation
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

#if canImport(FoundationModels)
@available(macOS 26, *)
func model() -> SystemLanguageModel {
    // Summaries and topic names transform the user's own text, which can be anything (code, logs, arguments in a
    // transcript); the default guardrails refuse too much of it.
    SystemLanguageModel(guardrails: .permissiveContentTransformations)
}

@available(macOS 26, *)
func unavailableReason(_ m: SystemLanguageModel) -> String? {
    switch m.availability {
    case .available: return nil
    case .unavailable(.deviceNotEligible): return "This Mac can't run Apple Intelligence."
    case .unavailable(.appleIntelligenceNotEnabled): return "Turn on Apple Intelligence in System Settings."
    case .unavailable(.modelNotReady): return "macOS is still downloading the Apple Intelligence model."
    case .unavailable(let other): return "Apple Intelligence isn't available (\(other))."
    }
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
    let m = model()
    if let why = unavailableReason(m) { fail(2, why) }
    let system = (req["system"] as? String).flatMap { $0.isEmpty ? nil : $0 }
    let prompt = req["prompt"] as? String ?? ""
    let options = GenerationOptions(temperature: req["temperature"] as? Double,
                                    maximumResponseTokens: req["max_tokens"] as? Int)
    let session = LanguageModelSession(model: m, instructions: system)
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
    let tooLong: (Int32, String) = (3, "The prompt is too long for the on-device model.")
    let declined: (Int32, String) = (4, "Apple Intelligence declined this text.")
    let missing: (Int32, String) = (2, "The Apple Intelligence model isn't available right now.")
    if #available(macOS 27, *) {
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

let command = CommandLine.arguments.dropFirst().first
#if canImport(FoundationModels)
if #available(macOS 26, *) {
    switch command {
    case "status":
        let why = unavailableReason(model())
        write(why.map { ["available": false, "reason": $0] } ?? ["available": true])
        exit(0)
    case "respond":
        let input = FileHandle.standardInput.readDataToEndOfFile()
        guard let req = (try? JSONSerialization.jsonObject(with: input)) as? [String: Any] else { fail(1, "expected a JSON request on stdin") }
        await respond(req)
    default:
        fail(1, "usage: pensieve-fm status | respond < request.json")
    }
}
#endif
if command == "status" {
    write(["available": false, "reason": "Apple Intelligence needs macOS 26 or later."])
    exit(0)
}
fail(2, "Apple Intelligence needs macOS 26 or later.")
