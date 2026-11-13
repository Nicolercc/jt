import SwiftUI

struct ReviewItem: Decodable {
    struct Candidate: Decodable, Identifiable {
        let id: Int
        let role: String
        let stage: String
    }

    struct Proposal: Decodable {
        let company: String?
        let kind: String?
        let evidence: String?
    }

    let resolved: Bool
    let subject: String?
    let gmailUrl: URL?
    let candidates: [Candidate]
    let proposal: Proposal?
}

struct ReviewSection: View {
    let reviewID: Int
    @State private var item: ReviewItem?
    @State private var selection: Int?
    @State private var busy = false
    @State private var recorded = false
    @State private var message = ""
    @AccessibilityFocusState private var statusFocused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Divider()
            Text("Recruiter update").font(.headline)
            if let item {
                if item.resolved {
                    Text("This update has been resolved.")
                } else {
                    Text(item.subject ?? "Recruiter email")
                    if let evidence = item.proposal?.evidence { Text(evidence) }
                    if let url = item.gmailUrl, url.scheme == "https", url.host == "mail.google.com" {
                        Link("Open source in Gmail", destination: url)
                    }
                    if let kind = item.proposal?.kind {
                        Text("Proposed update: \(kind == "screen" ? "Recruiter screen" : kind.capitalized)")
                    }
                    if item.candidates.isEmpty {
                        Text("No matching application was found. The proposal is still pending.")
                    } else {
                        Picker("Application at \(item.proposal?.company ?? "this company")", selection: $selection) {
                            Text("Choose an application").tag(nil as Int?)
                            ForEach(item.candidates) { candidate in
                                Text("\(candidate.role) · #\(candidate.id)").tag(Optional(candidate.id))
                            }
                        }
                        Button("Confirm update") { Task { await confirm() } }
                            .disabled(selection == nil || busy)
                    }
                    Button("Leave for later") {
                        self.item = nil
                        selection = nil
                        message = "Update is still pending."
                    }
                    .disabled(busy)
                }
            } else if !recorded {
                Button("Review update") { Task { await load() } }.disabled(busy)
            }
            if busy { ProgressView() }
            if !message.isEmpty {
                Text(message).accessibilityFocused($statusFocused)
            }
        }
    }

    private func load() async {
        guard !busy else { return }
        busy = true
        defer { busy = false }
        selection = nil
        do {
            item = try await request(["review", "show", String(reviewID), "--json"], as: ReviewItem.self)
            message = ""
        } catch {
            message = "Couldn’t load the update. Try again."
        }
    }

    private func confirm() async {
        guard !busy, let candidate = item?.candidates.first(where: { $0.id == selection }) else { return }
        busy = true
        defer { busy = false }
        let arguments = ["review", "approve", String(reviewID), "--app-id", String(candidate.id),
                         "--expected-stage", candidate.stage, "--json"]
        if let reply = try? await request(arguments, as: Reply.self), reply.ok {
            recorded = true
            message = "Update recorded for \(candidate.role)."
            item = nil
        } else {
            item = try? await request(["review", "show", String(reviewID), "--json"], as: ReviewItem.self)
            message = item?.resolved == true
                ? "This update has already been resolved. Check the application history."
                : "Couldn’t confirm the update. Review the current application before trying again."
        }
        selection = nil
        statusFocused = true
    }
}
