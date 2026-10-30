// One Loft per Windows user: a second start (a loft://pair link, the Start menu) hands its
// arguments to the running instance and exits.
using Microsoft.UI.Dispatching;
using Microsoft.UI.Xaml;
using Microsoft.Windows.AppLifecycle;

namespace Loft.App;

public static class Program
{
    [STAThread]
    private static int Main(string[] args)
    {
        WinRT.ComWrappersSupport.InitializeComWrappers();
        var me = AppInstance.FindOrRegisterForKey("no.incremental.loft.main");
        if (!me.IsCurrent)
        {
            var activation = AppInstance.GetCurrent().GetActivatedEventArgs();
            // Redirect off the STA thread, then wait (Windows App SDK's single-instance pattern).
            Task.Run(() => me.RedirectActivationToAsync(activation).AsTask()).Wait();
            return 0;
        }
        me.Activated += (_, a) => App.Instance?.OnRedirected(a);
        Application.Start(_p =>
        {
            var ctx = new DispatcherQueueSynchronizationContext(DispatcherQueue.GetForCurrentThread());
            SynchronizationContext.SetSynchronizationContext(ctx);
            _ = new App(args);
        });
        return 0;
    }
}
