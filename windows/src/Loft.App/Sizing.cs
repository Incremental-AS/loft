// Window sizes in effective pixels (scaled by the monitor's DPI), clamped to the work area, so no
// button ends up below the fold on a small or high-DPI screen.
using System.Runtime.InteropServices;
using Microsoft.UI.Windowing;

namespace Loft.App;

internal static class Sizing
{
    [DllImport("user32.dll")] private static extern uint GetDpiForWindow(nint hwnd);

    /// <summary>Sizes the window and centres it in the work area of its monitor.</summary>
    public static void Place(AppWindow w, int width, int height)
    {
        var hwnd = (nint)Microsoft.UI.Win32Interop.GetWindowFromWindowId(w.Id);
        var dpi = GetDpiForWindow(hwnd);
        var scale = dpi == 0 ? 1.0 : dpi / 96.0;
        var area = DisplayArea.GetFromWindowId(w.Id, DisplayAreaFallback.Primary).WorkArea;
        var cw = Math.Min((int)(width * scale), area.Width - 32);
        var ch = Math.Min((int)(height * scale), area.Height - 32);
        w.MoveAndResize(new Windows.Graphics.RectInt32(area.X + (area.Width - cw) / 2, area.Y + (area.Height - ch) / 2, cw, ch));
    }
}
