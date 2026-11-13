import SwiftUI

struct Reply: Decodable {
    var ok: Bool
    var state: String?
}

struct Snapshot: Decodable {
    struct Action: Decodable, Identifiable {
        struct Due: Decodable { var value: String }
        var actionId: Int
        var description: String
        var company: String
        var role: String
        var due: Due
        var gmailUrl: URL?
        var id: Int { actionId }
    }

    struct Freshness: Decodable {
        var gmailSyncedAt: String?
    }

    var schemaVersion: Int
    var generatedAt: String
    var actions: [Action]
    var freshness: Freshness
}

enum BackendFailure: Error {
    case unavailable
    case invalidResponse
}

func request<T: Decodable>(_ arguments: [String], as type: T.Type) async throws -> T {
    try await Task.detached {
        let environment = ProcessInfo.processInfo.environment
        guard let python = environment["JT_PYTHON"], let repo = environment["JT_REPO"] else {
            throw BackendFailure.unavailable
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = [URL(fileURLWithPath: repo).appendingPathComponent("jt.py").path] + arguments
        let output = Pipe()
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        try process.run()
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else { throw BackendFailure.invalidResponse }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(type, from: data)
    }.value
}

@MainActor
final class TodayModel: ObservableObject {
    @Published var snapshot: Snapshot?
    @Published var status = ""
    @Published var busy = false
    @Published var uncertain: Set<Int> = []

    private func load() async throws {
        let updated = try await request(["today", "--json"], as: Snapshot.self)
        guard updated.schemaVersion == 1 else { throw BackendFailure.invalidResponse }
        snapshot = updated
    }

    func refresh() async {
        guard !busy else { return }
        busy = true
        defer { busy = false }
        do {
            try await load()
            for id in uncertain {
                if (try? await actionState(id)) != nil {
                    uncertain.remove(id)
                }
            }
            status = uncertain.isEmpty ? "" : "Some completion results are still unknown."
        } catch {
            status = "Couldn’t refresh. Check that jt is running and try again."
        }
    }

    private func actionState(_ id: Int) async throws -> String? {
        try await request(["action", "show", String(id), "--json"], as: Reply.self).state
    }

    func complete(_ action: Snapshot.Action) async {
        guard !busy, !uncertain.contains(action.id) else { return }
        busy = true
        defer { busy = false }
        let reply = try? await request(["action", "done", String(action.id), "--json"], as: Reply.self)
        let state: String?
        if reply?.ok == true, reply?.state == "done" {
            state = "done"
        } else {
            state = try? await actionState(action.id)
        }
        guard state == "done" else {
            if state == nil { uncertain.insert(action.id) }
            status = state == nil
                ? "Completion couldn’t be confirmed. Refresh to check before trying again."
                : "jt didn’t record this as done. Current state: \(state ?? "")."
            return
        }
        snapshot?.actions.removeAll { $0.id == action.id }
        status = "Marked handled for \(action.company) · \(action.role)."
    }
}

@main
struct JToday: App {
    @StateObject private var model = TodayModel()

    var body: some Scene {
        MenuBarExtra("jt", systemImage: "tray") {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    Text("Today").font(.title2.bold())
                    if let snapshot = model.snapshot {
                        Text("Gmail last synced: \(snapshot.freshness.gmailSyncedAt ?? "Unknown")")
                            .font(.caption)
                        Text("Results loaded: \(snapshot.generatedAt)").font(.caption)
                        if snapshot.actions.isEmpty {
                            Text("No recruiter actions due today.")
                        }
                        ForEach(snapshot.actions.prefix(3)) { action in
                            Divider()
                            VStack(alignment: .leading, spacing: 6) {
                                Text(action.description).font(.headline)
                                Text("\(action.company) · \(action.role)")
                                Text("Due \(action.due.value)").font(.subheadline)
                                HStack {
                                    if let url = action.gmailUrl, url.scheme == "https",
                                       url.host == "mail.google.com" {
                                        Link("Open in Gmail", destination: url)
                                    }
                                    Button("Mark handled") {
                                        Task { await model.complete(action) }
                                    }
                                    .accessibilityLabel("Mark handled for \(action.company), \(action.role)")
                                    .disabled(model.busy || model.uncertain.contains(action.id))
                                }
                            }
                        }
                    } else if model.busy {
                        ProgressView("Loading recruiter actions")
                    }
                    if !model.status.isEmpty {
                        Text(model.status).accessibilityAddTraits(.updatesFrequently)
                    }
                    Button("Refresh") { Task { await model.refresh() } }
                        .disabled(model.busy)
                }
                .padding()
            }
            .frame(idealHeight: 340, maxHeight: 480)
            .frame(width: 360)
            .task { await model.refresh() }
        }
        .menuBarExtraStyle(.window)
    }
}
