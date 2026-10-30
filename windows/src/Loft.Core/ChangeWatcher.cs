// The change watcher (docs/design.md §D.2): long-poll /v1/changes and hand each page to the sync
// engine. Pure logic + callbacks, so the state machine is tested on Linux. Mirrors SyncLoop.swift,
// except that the page itself is passed on: on Windows the app applies it to the placeholders.
namespace Loft.Core;

public sealed class ChangeWatcher
{
    public GatewayClient Client { get; }
    public int Wait { get; set; } = 25;                     // gateway caps at 25 s (Cloudflare: 100)
    public TimeSpan MaxBackoff { get; set; } = TimeSpan.FromSeconds(60);
    public TimeSpan FirstBackoff { get; set; } = TimeSpan.FromSeconds(2);

    public ChangeWatcher(GatewayClient client) => Client = client;

    /// <summary>
    /// Runs until cancelled. <paramref name="onPage"/> gets every non-empty page and returns when it is
    /// applied (the anchor only moves on after that, so a crash replays the page). <paramref name="onResync"/>
    /// fires when the anchor is unknown or expired: "re-read everything". <paramref name="onAnchor"/> persists
    /// the anchor. <paramref name="onState"/> runs after every round.
    /// </summary>
    public async Task RunAsync(string? start,
                               Func<ChangesPage, Task> onPage,
                               Func<Task> onResync,
                               Action<string> onAnchor,
                               Action<LoopState> onState,
                               CancellationToken ct)
    {
        var anchor = start;
        var backoff = FirstBackoff;
        while (!ct.IsCancellationRequested)
        {
            try
            {
                if (anchor is null)
                {
                    anchor = await Client.LatestAnchorAsync(ct).ConfigureAwait(false);
                    await onResync().ConfigureAwait(false);          // we missed whatever happened before
                    onAnchor(anchor);
                }
                var page = await Client.ChangesAsync(anchor, Wait, ct: ct).ConfigureAwait(false);
                if (!page.IsEmpty) await onPage(page).ConfigureAwait(false);
                anchor = page.Anchor;
                onAnchor(anchor);
                onState(LoopState.Ok);
                backoff = FirstBackoff;
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested)
            {
                return;
            }
            catch (GatewayException e) when (e.Api?.IsAnchorExpired == true)
            {
                anchor = null;                                       // epoch changed / pruned: start over
            }
            catch (Exception e)
            {
                if (ct.IsCancellationRequested) return;
                onState(LoopState.From(e));
                try { await Task.Delay(backoff, ct).ConfigureAwait(false); }
                catch (OperationCanceledException) { return; }
                backoff = TimeSpan.FromTicks(Math.Min(backoff.Ticks * 2, MaxBackoff.Ticks));
            }
        }

    }
}
