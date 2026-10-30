// The app's UI strings (src/Loft.App/Strings.cs, Localization.cs and Strings/*.json, compiled in by the
// csproj). Every catalog dropped into Strings/ is checked here without any change to this file.
using System.Globalization;
using System.Reflection;
using System.Text;
using System.Text.RegularExpressions;
using Loft.App;
using Loft.Core;
using Xunit;

namespace Loft.Core.Tests;

public class StringsTests
{
    private static readonly Assembly Here = typeof(S).Assembly;
    private static readonly IReadOnlyList<string> Languages = Catalogs.Available(Here);
    private static IReadOnlyDictionary<string, string> Cat(string lang) => Catalogs.Load(Here, lang);
    private static readonly IReadOnlyDictionary<string, string> En = Cat("en");

    public static TheoryData<string> AllLanguages() => new(Languages);
    public static TheoryData<string> Translations() => new(Languages.Where(l => l != Catalogs.Source));

    private static Localizer Make(string lang, HashSet<string>? asked = null) =>
        new(lang, Cat(lang), En) { Asked = asked };

    // ── the catalogs ────────────────────────────────────────────────────

    [Fact]
    public void EnglishAndNorwegianAreEmbedded()
    {
        Assert.Contains("en", Languages);
        Assert.Contains("nb", Languages);
        Assert.True(En.Count > 50);
    }

    [Theory, MemberData(nameof(AllLanguages))]
    public void ValuesAreNonEmptyAndTrimmed(string lang)
    {
        foreach (var (k, v) in Cat(lang))
        {
            Assert.False(string.IsNullOrWhiteSpace(v), $"{lang}: {k} is empty");
            Assert.True(v == v.Trim(), $"{lang}: {k} has leading or trailing space");
            Assert.Matches(@"^[a-z0-9_]+(\.[a-z0-9_]+)+$", k);
        }
    }

    [Theory, MemberData(nameof(Translations))]
    public void TranslationKeysAreEnglishKeys(string lang)
    {
        var extra = Cat(lang).Keys.Except(En.Keys).ToList();
        Assert.True(extra.Count == 0, $"{lang}.json has keys en.json doesn't: {string.Join(", ", extra)}");
    }

    [Theory, MemberData(nameof(Translations))]
    public void PlaceholdersMatchEnglish(string lang)
    {
        foreach (var (k, v) in Cat(lang))
            Assert.True(Catalogs.Placeholders(En[k]).SequenceEqual(Catalogs.Placeholders(v)),
                $"{lang}: {k} has {{{string.Join("}, {", Catalogs.Placeholders(v))}}}, en has {{{string.Join("}, {", Catalogs.Placeholders(En[k]))}}}");
    }

    [Theory, MemberData(nameof(AllLanguages))]
    public void PluralKeysComeInPairs(string lang)
    {
        var c = Cat(lang);
        foreach (var k in c.Keys)
        {
            if (k.EndsWith(".one")) Assert.True(c.ContainsKey(k[..^4] + ".other"), $"{lang}: {k} without .other");
            if (k.EndsWith(".other")) Assert.True(c.ContainsKey(k[..^6] + ".one"), $"{lang}: {k} without .one");
        }
    }

    [Fact]
    public void NorwegianIsComplete()
    {
        var missing = En.Keys.Except(Cat("nb").Keys).ToList();
        Assert.True(missing.Count == 0, "nb.json lacks: " + string.Join(", ", missing));
    }

    [Theory, MemberData(nameof(AllLanguages))]
    public void EveryCatalogIsPickedByItsOwnCulture(string lang) =>
        Assert.Equal(lang, Catalogs.Resolve(CultureInfo.GetCultureInfo(lang), Languages));

    [Fact]
    public void ParserSkipsNotesAndRefusesDuplicatesAndNonStrings()
    {
        static Dictionary<string, string> P(string json) => Catalogs.Parse(new MemoryStream(Encoding.UTF8.GetBytes(json)));
        var d = P("""{"_note": "machine translated, 2026-10-30", "_meta": {"x": 1}, "a.b": "C"}""");
        Assert.Equal(new[] { "a.b" }, d.Keys);
        Assert.Throws<FormatException>(() => P("""{"a.b": "x", "a.b": "y"}"""));
        Assert.Throws<FormatException>(() => P("""{"a.b": 1}"""));
        Assert.Throws<FormatException>(() => P("""["a"]"""));
    }

    // ── S: every member, every language ─────────────────────────────────

    private static IEnumerable<object?[]> ArgsFor(ParameterInfo[] ps)
    {
        IEnumerable<object?[]> acc = new[] { Array.Empty<object?>() };
        foreach (var p in ps)
        {
            object?[] values = p.ParameterType switch
            {
                var t when t == typeof(int) => new object?[] { 0, 1, 2, 5, 21 },
                var t when t == typeof(long) => new object?[] { 5L << 30, 500L << 20, 1L << 30 },
                var t when t == typeof(bool) => new object?[] { true, false },
                var t when t == typeof(string) => new object?[] { "X" },
                var t when t == typeof(SyncError) => Enum.GetValues<SyncErrorKind>().Select(k => (object?)new SyncError(k, "x")).ToArray(),
                var t when t.IsEnum => Enum.GetValues(t).Cast<object?>().ToArray(),
                var t => throw new InvalidOperationException($"StringsTests: teach ArgsFor about {t.Name} ({p.Member.Name})"),
            };
            acc = acc.SelectMany(a => values.Select(v => a.Append(v).ToArray())).ToList();
        }
        return acc;
    }

    /// <summary>Calls every public string member of S (all argument combinations) under <paramref name="loc"/>.</summary>
    private static List<(string Member, string Text)> Render(Localizer loc)
    {
        var res = new List<(string, string)>();
        using var _ = S.Use(loc);
        foreach (var p in typeof(S).GetProperties(BindingFlags.Public | BindingFlags.Static).Where(p => p.PropertyType == typeof(string)))
            res.Add((p.Name, (string)p.GetValue(null)!));
        foreach (var m in typeof(S).GetMethods(BindingFlags.Public | BindingFlags.Static).Where(m => m.ReturnType == typeof(string) && !m.IsSpecialName))
            foreach (var args in ArgsFor(m.GetParameters()))
                res.Add(($"{m.Name}({string.Join(", ", args)})", (string)m.Invoke(null, args)!));
        return res;
    }

    [Theory, MemberData(nameof(AllLanguages))]
    public void EveryMemberOfSHasItsText(string lang)
    {
        foreach (var (member, text) in Render(Make(lang)))
        {
            Assert.False(text.Contains('⟦'), $"{lang}: S.{member} = {text}");
            Assert.False(Regex.IsMatch(text, @"\{[A-Za-z_]"), $"{lang}: S.{member} left a placeholder: {text}");
            Assert.False(string.IsNullOrWhiteSpace(text), $"{lang}: S.{member} is empty");
        }
    }

    [Fact]
    public void EveryEnglishKeyIsUsed()
    {
        var asked = new HashSet<string>();
        Render(Make("en", asked));
        var unused = En.Keys.Except(asked).ToList();
        Assert.True(unused.Count == 0, "keys no S member asks for: " + string.Join(", ", unused));
    }

    [Fact]
    public void RendersAsBefore()
    {
        using (S.Use(Make("en")))
        {
            Assert.Equal("Syncing 1 file · Home", S.Syncing(1, Route.Lan, true));
            Assert.Equal("Syncing 3 files · Away", S.Syncing(3, Route.Public, true));
            Assert.Equal("Paused · 2 changes waiting to upload", S.Paused(2));
            Assert.Equal("This PC is paired with A. Pairing with B signs you out of A on this PC.", S.ReplacePairing("A", "B"));
            Assert.Equal("5 GB", S.Gb(5L << 30));
            Assert.Equal("500 MB", S.Gb(500L << 20));
        }
        using (S.Use(Make("nb")))
        {
            Assert.Equal("nb", S.Language);
            Assert.Equal("Synkroniserer 1 fil · Hjemme", S.Syncing(1, Route.Lan, true));
            Assert.Equal("På pause · 0 endringer venter på opplasting", S.Paused(0));
            Assert.Equal("Borte: Ikke satt opp (bare hjemmenettet)", S.HomeOnly);
            Assert.Equal("Logg inn igjen · Passordet på NAS-en er endret", S.Error(new SyncError(SyncErrorKind.NasPasswordChanged)));
        }
    }

    // ── fallback ────────────────────────────────────────────────────────

    [Fact]
    public void AMissingKeyFallsBackToEnglishWithEnglishPlurals()
    {
        var fr = new Localizer("fr", new Dictionary<string, string> { ["tray.cant_reach_nas"] = "Impossible de joindre le NAS. Nouvel essai…" }, En);
        using var _ = S.Use(fr);
        Assert.Equal("Impossible de joindre le NAS. Nouvel essai…", S.CantReachNas);
        Assert.Equal("Can't reach home. Retrying…", S.CantReachHome);
        // fr says 0 is "one", but the English text it falls back to must follow the English rule.
        Assert.Equal("0 changes waiting to upload", S.Waiting(0));
        Assert.Equal(Localizer.Missing("no.such_key"), fr.Text("no.such_key"));
    }

    [Fact]
    public void FrenchPluralUsesItsOwnRule()
    {
        var fr = new Localizer("fr", new Dictionary<string, string> { ["tray.waiting.one"] = "{n} modification", ["tray.waiting.other"] = "{n} modifications" }, En);
        Assert.Equal("0 modification", fr.Plural("tray.waiting", 0));
        Assert.Equal("1 modification", fr.Plural("tray.waiting", 1));
        Assert.Equal("2 modifications", fr.Plural("tray.waiting", 2));
    }

    [Fact]
    public void UnknownPlaceholdersStay() =>
        Assert.Equal("a {b} c", new Localizer("en", new Dictionary<string, string> { ["k.k"] = "{a} {b} c" }, En).Text("k.k", ("a", "a")));

    // ── rules ───────────────────────────────────────────────────────────

    [Theory]
    [InlineData("en", 0, "other"), InlineData("en", 1, "one"), InlineData("en", 2, "other"), InlineData("en", 21, "other")]
    [InlineData("nb", 0, "other"), InlineData("nb", 1, "one"), InlineData("nb", 2, "other")]
    [InlineData("de", 1, "one"), InlineData("de", 0, "other"), InlineData("es", 1, "one"), InlineData("es", 2, "other")]
    [InlineData("it", 1, "one"), InlineData("it", 0, "other"), InlineData("nl", 1, "one"), InlineData("nl", 11, "other")]
    [InlineData("fr", 0, "one"), InlineData("fr", 1, "one"), InlineData("fr", 2, "other"), InlineData("fr", 100, "other")]
    [InlineData("pt-BR", 0, "one"), InlineData("pt-BR", 1, "one"), InlineData("pt-BR", 2, "other")]
    [InlineData("ja", 0, "other"), InlineData("ja", 1, "other"), InlineData("ja", 2, "other")]
    [InlineData("ko", 1, "other"), InlineData("zh-Hans", 1, "other"), InlineData("zh-Hans", 0, "other")]
    [InlineData("sv", 1, "one"), InlineData("sv", 0, "other")]
    public void PluralCategory(string lang, long n, string expected) => Assert.Equal(expected, PluralRules.Category(lang, n));

    private static readonly string[] Future = { "en", "nb", "de", "es", "fr", "it", "nl", "pt-BR", "ja", "ko", "zh-Hans" };

    [Theory]
    [InlineData("en-US", "en"), InlineData("en-GB", "en"), InlineData("", "en"), InlineData("sv-SE", "en"), InlineData("se-NO", "en")]
    [InlineData("nb-NO", "nb"), InlineData("nb", "nb"), InlineData("nn-NO", "nb"), InlineData("nn", "nb"), InlineData("no", "nb")]
    [InlineData("de-DE", "de"), InlineData("de-AT", "de"), InlineData("de-CH", "de"), InlineData("es-MX", "es"), InlineData("fr-CA", "fr")]
    [InlineData("it-IT", "it"), InlineData("nl-BE", "nl"), InlineData("ja-JP", "ja"), InlineData("ko-KR", "ko")]
    [InlineData("pt-BR", "pt-BR"), InlineData("pt", "pt-BR"), InlineData("pt-PT", "pt-BR")]
    [InlineData("zh-CN", "zh-Hans"), InlineData("zh-SG", "zh-Hans"), InlineData("zh-Hans", "zh-Hans"), InlineData("zh", "zh-Hans")]
    [InlineData("zh-TW", "en"), InlineData("zh-HK", "en"), InlineData("zh-Hant", "en")]
    public void ResolvesTheDisplayLanguage(string culture, string expected) =>
        Assert.Equal(expected, Catalogs.Resolve(CultureInfo.GetCultureInfo(culture), Future));

    [Theory]
    [InlineData("de-DE"), InlineData("fr-FR"), InlineData("zh-CN"), InlineData("pt-BR")]
    public void ALanguageWithoutACatalogGetsEnglish(string culture) =>
        Assert.Equal("en", Catalogs.Resolve(CultureInfo.GetCultureInfo(culture), new[] { "en", "nb" }));

    [Fact]
    public void ResolutionIgnoresCase() =>
        Assert.Equal("pt-BR", Catalogs.Resolve(CultureInfo.GetCultureInfo("pt-br"), new[] { "en", "pt-BR" }));
}
