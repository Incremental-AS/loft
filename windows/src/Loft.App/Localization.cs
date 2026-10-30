// The machinery behind S: string catalogs, language resolution and plural rules. The catalogs are
// Strings/<language>.json, embedded as "Loft.Strings.<language>.json" by a wildcard in the csproj, so a
// new language is a new file and nothing else (Strings/README.md). Platform-neutral on purpose: the
// Linux test run (tests/Loft.Core.Tests) compiles this file and the catalogs too.
using System.Globalization;
using System.Reflection;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace Loft.App;

/// <summary>The embedded catalogs and how a Windows display language picks one.</summary>
public static class Catalogs
{
    /// <summary>The source language. Every key exists here; any other catalog may lack keys.</summary>
    public const string Source = "en";
    public const string ResourcePrefix = "Loft.Strings.";
    public const string ResourceSuffix = ".json";

    /// <summary>Language codes of the catalogs embedded in <paramref name="assembly"/> ("en", "nb", "pt-BR" …).</summary>
    public static IReadOnlyList<string> Available(Assembly assembly) =>
        assembly.GetManifestResourceNames()
            .Where(n => n.StartsWith(ResourcePrefix, StringComparison.Ordinal) && n.EndsWith(ResourceSuffix, StringComparison.Ordinal))
            .Select(n => n[ResourcePrefix.Length..^ResourceSuffix.Length])
            .Order(StringComparer.Ordinal)
            .ToList();

    /// <summary>One catalog, or an empty one if it isn't embedded.</summary>
    public static IReadOnlyDictionary<string, string> Load(Assembly assembly, string language)
    {
        using var s = assembly.GetManifestResourceStream(ResourcePrefix + language + ResourceSuffix);
        return s is null ? new Dictionary<string, string>() : Parse(s);
    }

    /// <summary>Keys starting with "_" ("_note" from the translation tool) are notes, never UI text.</summary>
    public static bool IsMeta(string key) => key.StartsWith('_');

    /// <summary>
    /// A flat JSON object of string values, minus "_" keys. Throws on anything else, or on a duplicate key.
    /// </summary>
    public static Dictionary<string, string> Parse(Stream json)
    {
        using var doc = JsonDocument.Parse(json, new JsonDocumentOptions { CommentHandling = JsonCommentHandling.Disallow });
        if (doc.RootElement.ValueKind != JsonValueKind.Object) throw new FormatException("a catalog is one JSON object");
        var d = new Dictionary<string, string>(StringComparer.Ordinal);
        foreach (var p in doc.RootElement.EnumerateObject())
        {
            if (IsMeta(p.Name)) continue;
            if (p.Value.ValueKind != JsonValueKind.String) throw new FormatException($"{p.Name}: not a string");
            if (!d.TryAdd(p.Name, p.Value.GetString()!)) throw new FormatException($"{p.Name}: duplicate key");
        }
        return d;
    }

    /// <summary>
    /// The catalog for a display language: the culture's own name, then its parents (nb-NO → nb,
    /// zh-CN → zh-Hans, pt-BR stays pt-BR), with nb, nn and no all meaning nb, any other Chinese
    /// than Traditional meaning zh-Hans, and plain pt meaning pt-BR. Nothing matching → en.
    /// </summary>
    public static string Resolve(CultureInfo culture, IReadOnlyCollection<string> available)
    {
        string? Have(string name) => available.FirstOrDefault(a => string.Equals(a, name, StringComparison.OrdinalIgnoreCase));

        // zh-TW's parent chain ends in plain "zh"; that must not turn Traditional into Simplified.
        var traditional = culture.Name.Split('-').Skip(1).Any(p => p.ToUpperInvariant() is "HANT" or "TW" or "HK" or "MO");
        for (var c = culture; !string.IsNullOrEmpty(c.Name); c = c.Parent)
        {
            if (Have(c.Name) is { } exact) return exact;
            if (Alias(c.Name, traditional) is { } alias && Have(alias) is { } aliased) return aliased;
            if (c.Parent.Name == c.Name) break;
        }
        return Source;
    }

    private static string? Alias(string name, bool traditional)
    {
        var parts = name.Split('-');
        return parts[0].ToLowerInvariant() switch
        {
            "nb" or "nn" or "no" => "nb",
            "pt" when parts.Length == 1 => "pt-BR",
            "zh" when !traditional => "zh-Hans",
            _ => null,
        };
    }

    private static readonly Regex PlaceholderRx = new(@"\{([A-Za-z_][A-Za-z0-9_]*)\}", RegexOptions.CultureInvariant);

    /// <summary>The distinct {name} placeholders in a value, sorted.</summary>
    public static IReadOnlyList<string> Placeholders(string value) =>
        PlaceholderRx.Matches(value).Select(m => m.Groups[1].Value).Distinct().Order(StringComparer.Ordinal).ToList();

    internal static string Fill(string value, IReadOnlyList<(string Name, object? Value)> args)
    {
        if (args.Count == 0) return value;
        return PlaceholderRx.Replace(value, m =>
        {
            foreach (var (name, v) in args)
                if (name == m.Groups[1].Value) return Convert.ToString(v, CultureInfo.CurrentCulture) ?? "";
            return m.Value;
        });
    }
}

/// <summary>
/// CLDR cardinal plural categories, cut down to what Loft's catalogs use: "one" or "other". Integer
/// counts only. A language not listed here gets the English rule.
/// </summary>
public static class PluralRules
{
    public const string One = "one";
    public const string Other = "other";

    public static string Category(string language, long n)
    {
        var lang = language.Split('-')[0].ToLowerInvariant();
        return lang switch
        {
            "ja" or "ko" or "zh" => Other,
            "fr" or "pt" => n is 0 or 1 ? One : Other,     // pt here means pt-BR; pt-PT would be n == 1
            _ => n == 1 ? One : Other,                     // en, nb, de, es, it, nl
        };
    }
}

/// <summary>One language's strings, with English behind it for keys the language doesn't have yet.</summary>
public sealed class Localizer
{
    private readonly IReadOnlyDictionary<string, string> _strings;
    private readonly IReadOnlyDictionary<string, string> _source;

    public Localizer(string language, IReadOnlyDictionary<string, string> strings, IReadOnlyDictionary<string, string> source)
    {
        Language = language;
        _strings = strings;
        _source = source;
    }

    /// <summary>The catalog in use ("en", "nb", "pt-BR" …).</summary>
    public string Language { get; }

    /// <summary>Every key asked for since this localizer was made (the tests use it to find unused keys).</summary>
    internal HashSet<string>? Asked { get; init; }

    /// <summary>What a key missing everywhere, even in English, shows as. A bug; the tests look for it.</summary>
    public static string Missing(string key) => "⟦" + key + "⟧";

    public static Localizer For(Assembly assembly, CultureInfo culture)
    {
        var language = Catalogs.Resolve(culture, Catalogs.Available(assembly));
        var source = Catalogs.Load(assembly, Catalogs.Source);
        return new Localizer(language, language == Catalogs.Source ? source : Catalogs.Load(assembly, language), source);
    }

    public string Text(string key, params (string Name, object? Value)[] args)
    {
        Asked?.Add(key);
        var value = _strings.TryGetValue(key, out var v) ? v : _source.TryGetValue(key, out var e) ? e : Missing(key);
        return Catalogs.Fill(value, args);
    }

    /// <summary>"<paramref name="key"/>.one" or ".other" by this language's rule, with {n} filled in.</summary>
    public string Plural(string key, long n, params (string Name, object? Value)[] args)
    {
        var all = new (string, object?)[args.Length + 1];
        all[0] = ("n", n);
        args.CopyTo(all, 1);
        var own = $"{key}.{PluralRules.Category(Language, n)}";
        var english = $"{key}.{PluralRules.Category(Catalogs.Source, n)}";
        Asked?.Add(key + "." + PluralRules.One);
        Asked?.Add(key + "." + PluralRules.Other);
        // A language without this key falls back to English as a whole, English plural rule included.
        var value = _strings.TryGetValue(own, out var v) ? v : _source.TryGetValue(english, out var e) ? e : Missing(own);
        return Catalogs.Fill(value, all);
    }
}
