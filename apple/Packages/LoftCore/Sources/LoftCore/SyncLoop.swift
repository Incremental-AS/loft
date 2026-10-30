// The host app's change watcher (docs/design.md §D.2): long-poll /v1/changes and tell the
// system when something moved. Pure logic + a callback for the platform side
// (signalEnumerator), so the state machine is tested on Linux.
import Foundation

/// What the menu should say after one round of the loop.
public enum LoopState: Equatable, Sendable {
    case ok                    // the gateway answered and the NAS is there
    case cantReachHome         // no answer from the gateway (or Cloudflare without it)
    case cantReachNAS          // gateway up, NAS behind it down
    case failed(SyncError)     // stopped until the user acts (or the cause clears)

    public init(_ error: Error) {
        switch error {
        case let g as GatewayError:
            switch g {
            case .unreachable: self = .cantReachHome
            case .notSignedIn, .notPaired: self = .failed(.signInExpired)
            case .api(let e):
                if e.isNASUnreachable { self = .cantReachNAS }
                else if let s = SyncError(syncLoopFailure: e) { self = .failed(s) }
                else { self = .ok }                   // anchor_expired: handled by the loop
            case .badResponse(let s, _): self = .failed(.stopped(code: "bad_response_\(s)"))
            case .changedDuringTransfer: self = .ok
            }
        default:
            self = .cantReachHome                     // URLError and friends
        }
    }
}

public final class ChangeWatcher: @unchecked Sendable {
    public let client: GatewayClient
    public var wait = LoftConfig.longPollWait
    /// Backoff after a failed round: 2, 4, 8 … 60 s.
    public var maxBackoff: Double = 60

    public init(client: GatewayClient) { self.client = client }

    /// Runs until cancelled. `onChange` fires whenever the feed returned anything (or the
    /// anchor expired, which means "re-read everything"); `onState` after every round.
    public func run(from start: String?,
                    onChange: @escaping @Sendable () async -> Void,
                    onState: @escaping @Sendable (LoopState) async -> Void) async {
        var anchor = start
        var backoff = 2.0
        while !Task.isCancelled {
            do {
                if anchor == nil {
                    anchor = try await client.latestAnchor()
                    await onChange()                 // we missed whatever happened before
                }
                let page = try await client.changes(anchor: anchor!, wait: wait)
                anchor = page.anchor
                if !page.isEmpty { await onChange() }
                await onState(.ok)
                backoff = 2
            } catch is CancellationError {
                return
            } catch GatewayError.api(let e) where e.isAnchorExpired {
                anchor = nil                         // epoch changed / pruned: start over
                continue
            } catch {
                if Task.isCancelled { return }
                await onState(LoopState(error))
                try? await Task.sleep(nanoseconds: UInt64(backoff * 1e9))
                backoff = min(backoff * 2, maxBackoff)
            }
        }
    }

}
