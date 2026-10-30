// %LOCALAPPDATA%\Loft\loft.log. Never a password or a token (the client code never formats them).
namespace Loft.App;

public static class Log
{
    private static readonly object Lock = new();
    public static string Path { get; } =
        System.IO.Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Loft", "loft.log");

    public static void Write(string line)
    {
        try
        {
            lock (Lock)
            {
                Directory.CreateDirectory(System.IO.Path.GetDirectoryName(Path)!);
                var fi = new FileInfo(Path);
                if (fi.Exists && fi.Length > 5 * 1024 * 1024) File.Move(Path, Path + ".1", overwrite: true);
                File.AppendAllText(Path, $"{DateTime.Now:yyyy-MM-dd HH:mm:ss} {line}{Environment.NewLine}");
            }
        }
        catch (IOException) { }
    }
}
