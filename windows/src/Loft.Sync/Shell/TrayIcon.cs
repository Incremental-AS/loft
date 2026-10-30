// The notification-area (tray) icon, straight on Shell_NotifyIcon: a hidden window receives the
// icon's messages, and the menu is a native popup menu. Create it on a thread that pumps window
// messages (the WinUI UI thread does).
using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;
using System.Runtime.Versioning;
using Windows.Win32;
using Windows.Win32.Foundation;
using Windows.Win32.UI.Shell;
using Windows.Win32.UI.WindowsAndMessaging;

namespace Loft.Sync.Shell;

public sealed record TrayMenuItem(string Text, Action? OnClick = null, bool Enabled = true, bool Checked = false)
{
    public static readonly TrayMenuItem Separator = new("-");
    public bool IsSeparator => Text == "-";
}

[SupportedOSPlatform("windows6.1")]
public sealed unsafe class TrayIcon : IDisposable
{
    private const string ClassName = "LoftTrayWindow";
    private const uint CallbackMessage = PInvoke.WM_APP + 1;
    private const uint NIN_KEYSELECT = PInvoke.NIN_SELECT | 0x1;
    private const uint TPM_RIGHTBUTTON = 0x0002, TPM_NONOTIFY = 0x0080, TPM_RETURNCMD = 0x0100;

    private static readonly Dictionary<nint, TrayIcon> ByWindow = new();
    private static ushort _atom;
    private static uint _taskbarCreated;

    private readonly HWND _hwnd;
    private HICON _icon;
    private string _tooltip;
    private bool _added;

    /// <summary>Builds the menu each time it opens, so it always shows the current state.</summary>
    public Func<IReadOnlyList<TrayMenuItem>>? Menu { get; set; }
    public event Action<string>? Error;
    /// <summary>Light/dark changed (Settings → Personalisation → Colours): pick the other tray glyphs.</summary>
    public event Action? ThemeChanged;
    /// <summary>Windows is signing out or shutting down: save state now (the process may not get another chance).</summary>
    public event Action? SessionEnding;

    public TrayIcon(string iconPath, string tooltip)
    {
        EnsureClass();
        _hwnd = PInvoke.CreateWindowEx(0, ClassName, "Loft", WINDOW_STYLE.WS_OVERLAPPED, 0, 0, 0, 0,
                                       HWND.Null, null, null, null);
        if (_hwnd.IsNull) throw new InvalidOperationException($"tray window: win32 error {Marshal.GetLastWin32Error()}");
        ByWindow[(nint)_hwnd.Value] = this;
        _tooltip = tooltip;
        _icon = LoadIcon(iconPath);
        Add();
    }

    private static void EnsureClass()
    {
        if (_atom != 0) return;
        var module = PInvoke.GetModuleHandle((string?)null);
        fixed (char* cn = ClassName)
        {
            var wc = new WNDCLASSEXW
            {
                cbSize = (uint)sizeof(WNDCLASSEXW),
                lpfnWndProc = &WndProc,
                hInstance = new HINSTANCE(module.DangerousGetHandle()),
                lpszClassName = new PCWSTR(cn),
            };
            _atom = PInvoke.RegisterClassEx(&wc);
        }
        if (_atom == 0) throw new InvalidOperationException($"tray class: win32 error {Marshal.GetLastWin32Error()}");
        _taskbarCreated = PInvoke.RegisterWindowMessage("TaskbarCreated");
    }

    private static HICON LoadIcon(string path)
    {
        var cx = PInvoke.GetSystemMetrics(SYSTEM_METRICS_INDEX.SM_CXSMICON);
        var cy = PInvoke.GetSystemMetrics(SYSTEM_METRICS_INDEX.SM_CYSMICON);
        fixed (char* p = path)
        {
            var h = PInvoke.LoadImage(HINSTANCE.Null, new PCWSTR(p), GDI_IMAGE_TYPE.IMAGE_ICON, cx, cy, IMAGE_FLAGS.LR_LOADFROMFILE);
            return new HICON(h.Value);
        }
    }

    private NOTIFYICONDATAW Data(NOTIFY_ICON_DATA_FLAGS flags)
    {
        var nid = new NOTIFYICONDATAW
        {
            cbSize = (uint)sizeof(NOTIFYICONDATAW),
            hWnd = _hwnd,
            uID = 1,
            uFlags = flags,
            uCallbackMessage = CallbackMessage,
            hIcon = _icon,
        };
        var tip = _tooltip.Length > 127 ? _tooltip[..127] : _tooltip;
        var dst = new Span<char>(nid.szTip.Value, 128);
        tip.AsSpan().CopyTo(dst);
        dst[tip.Length] = '\0';
        return nid;
    }

    private void Add()
    {
        var nid = Data(NOTIFY_ICON_DATA_FLAGS.NIF_MESSAGE | NOTIFY_ICON_DATA_FLAGS.NIF_ICON | NOTIFY_ICON_DATA_FLAGS.NIF_TIP
                       | NOTIFY_ICON_DATA_FLAGS.NIF_SHOWTIP);
        _added = PInvoke.Shell_NotifyIcon(NOTIFY_ICON_MESSAGE.NIM_ADD, &nid);
        if (!_added) { Error?.Invoke("Shell_NotifyIcon(NIM_ADD) failed"); return; }
        nid.uVersion = 4;                                        // NOTIFYICON_VERSION_4
        PInvoke.Shell_NotifyIcon(NOTIFY_ICON_MESSAGE.NIM_SETVERSION, &nid);
    }

    /// <summary>New state icon and/or tooltip.</summary>
    public void Update(string? iconPath, string tooltip)
    {
        if (iconPath is not null)
        {
            var old = _icon;
            _icon = LoadIcon(iconPath);
            if (!old.IsNull) PInvoke.DestroyIcon(old);
        }
        _tooltip = tooltip;
        var nid = Data(NOTIFY_ICON_DATA_FLAGS.NIF_ICON | NOTIFY_ICON_DATA_FLAGS.NIF_TIP | NOTIFY_ICON_DATA_FLAGS.NIF_SHOWTIP);
        if (!PInvoke.Shell_NotifyIcon(NOTIFY_ICON_MESSAGE.NIM_MODIFY, &nid)) Add();
    }

    private void ShowMenu()
    {
        var items = Menu?.Invoke() ?? Array.Empty<TrayMenuItem>();
        if (items.Count == 0) return;
        var menu = PInvoke.CreatePopupMenu();
        try
        {
            for (var i = 0; i < items.Count; i++)
            {
                var it = items[i];
                if (it.IsSeparator)
                {
                    PInvoke.AppendMenu(menu, MENU_ITEM_FLAGS.MF_SEPARATOR, 0, default(PCWSTR));
                    continue;
                }
                var flags = MENU_ITEM_FLAGS.MF_STRING;
                if (!it.Enabled || it.OnClick is null) flags |= MENU_ITEM_FLAGS.MF_GRAYED;
                if (it.Checked) flags |= MENU_ITEM_FLAGS.MF_CHECKED;
                fixed (char* t = it.Text) PInvoke.AppendMenu(menu, flags, (nuint)(i + 1), new PCWSTR(t));
            }
            PInvoke.GetCursorPos(out var pt);
            // Without the foreground the menu wouldn't close on a click elsewhere.
            if (!PInvoke.SetForegroundWindow(_hwnd)) Error?.Invoke("SetForegroundWindow failed before the menu");
            var cmd = PInvoke.TrackPopupMenuEx(menu, TPM_RIGHTBUTTON | TPM_NONOTIFY | TPM_RETURNCMD, pt.X, pt.Y, _hwnd, null);
            PInvoke.PostMessage(_hwnd, PInvoke.WM_NULL, default, default);
            var chosen = (int)cmd.Value;
            if (chosen > 0 && chosen <= items.Count) items[chosen - 1].OnClick?.Invoke();
        }
        catch (Exception e) { Error?.Invoke(e.Message); }
        finally { PInvoke.DestroyMenu(menu); }
    }

    [UnmanagedCallersOnly(CallConvs = new[] { typeof(CallConvStdcall) })]
    private static LRESULT WndProc(HWND hwnd, uint msg, WPARAM wParam, LPARAM lParam)
    {
        try
        {
            if (ByWindow.TryGetValue((nint)hwnd.Value, out var self))
            {
                if (msg == CallbackMessage)
                {
                    var ev = (uint)(lParam.Value & 0xFFFF);
                    if (ev is PInvoke.WM_CONTEXTMENU or PInvoke.NIN_SELECT or NIN_KEYSELECT or PInvoke.WM_LBUTTONUP)
                        self.ShowMenu();
                    return new LRESULT(0);
                }
                if (msg == PInvoke.WM_SETTINGCHANGE && lParam.Value != 0
                    && new string((char*)lParam.Value) == "ImmersiveColorSet")
                {
                    self.ThemeChanged?.Invoke();
                    return new LRESULT(0);
                }
                if (msg == PInvoke.WM_ENDSESSION && wParam.Value != 0)
                {
                    self.SessionEnding?.Invoke();
                    return new LRESULT(0);
                }
                if (msg == _taskbarCreated && _taskbarCreated != 0)
                {
                    self.Add();                                  // Explorer restarted
                    return new LRESULT(0);
                }
            }
        }
        catch { /* never across the native boundary */ }
        return PInvoke.DefWindowProc(hwnd, msg, wParam, lParam);
    }

    public void Dispose()
    {
        if (_added)
        {
            var nid = Data(0);
            PInvoke.Shell_NotifyIcon(NOTIFY_ICON_MESSAGE.NIM_DELETE, &nid);
            _added = false;
        }
        if (!_icon.IsNull) PInvoke.DestroyIcon(_icon);
        ByWindow.Remove((nint)_hwnd.Value);
        PInvoke.DestroyWindow(_hwnd);
    }
}
