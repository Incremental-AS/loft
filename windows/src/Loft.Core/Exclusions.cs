// Names that never leave this PC (docs/design.md §D.3). Same list as Exclusions.swift, plus the
// temp files Office for Windows writes during a safe-save. The gateway refuses most of these
// too, as a second wall.
using System.Text.RegularExpressions;

namespace Loft.Core;

public static partial class Exclusions
{
    private static readonly string[] Prefixes = { "~$", "._", ".~lock.", "Word Work File ", "~WRL", "~WRD", ".loft" };
    private static readonly HashSet<string> Exact = new(StringComparer.OrdinalIgnoreCase)
        { ".DS_Store", "Icon\r", "Thumbs.db", "desktop.ini" };

    /// <summary>Excel for Windows saves through an 8-hex-digit temp file with no extension.</summary>
    [GeneratedRegex("^[0-9A-F]{8}$")]
    private static partial Regex ExcelTemp();

    public static bool IsExcluded(string name)
    {
        if (Exact.Contains(name)) return true;
        foreach (var p in Prefixes)
            if (name.StartsWith(p, StringComparison.Ordinal)) return true;
        if (name.EndsWith(".tmp", StringComparison.OrdinalIgnoreCase)) return true;
        if (name.Contains(".sb-", StringComparison.Ordinal)) return true;   // NSDocument safe-save scratch
        if (ExcelTemp().IsMatch(name)) return true;
        return false;
    }
}
