package no.incremental.loft.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/** Parser only: tests never fetch releases.json. */
class UpdateCheckTest {
    @Test fun newerVersion() {
        val u = UpdateCheck.parse("""{"android":{"version":"0.2.1","version_code":2,"url":"https://loft.incremental.no/download/#android"}}""", 1)
        assertEquals(UpdateInfo("0.2.1", 2, "https://loft.incremental.no/download/#android"), u)
    }

    @Test fun platformsKeyAndOtherPlatforms() {
        // The site's flat form with every platform, and the nested "platforms" form (docs/releases-json.md).
        val flat = """{"android":{"version":"0.2.1","version_code":2,"url":"https://loft.incremental.no/download/#android","sha256":"ab"},
            "windows":{"version":"0.2.1","url":"https://example.org/x.zip"},"linux":null,"macos":{"version":"0.2.1"}}"""
        assertEquals(2L, UpdateCheck.parse(flat, 1)!!.versionCode)
        val nested = """{"platforms":{"android":{"version":"0.2.1","version_code":2}}}"""
        assertEquals(UpdateInfo("0.2.1", 2, UpdateCheck.DOWNLOAD_PAGE), UpdateCheck.parse(nested, 1))
        assertNull(UpdateCheck.parse("""{"android":null,"windows":{"version":"9"}}""", 1))
    }

    @Test fun sameOrOlderIsNothing() {
        assertNull(UpdateCheck.parse("""{"android":{"version":"0.2.0","version_code":1}}""", 1))
        assertNull(UpdateCheck.parse("""{"android":{"version":"0.1.0","version_code":0}}""", 1))
    }

    @Test fun garbageIsNothing() {
        assertNull(UpdateCheck.parse("<html>", 1))
        assertNull(UpdateCheck.parse("""{"windows":{"version":"9","version_code":9}}""", 1))
        assertNull(UpdateCheck.parse("""{"android":{"version":"9"}}""", 1))
        assertNull(UpdateCheck.parse("""{"android":{"version_code":"x","version":"9"}}""", 1))
    }

    @Test fun onlyOurOwnPagesOpen() {
        val other = UpdateCheck.parse("""{"android":{"version":"1","version_code":9,"url":"https://evil.example/apk"}}""", 1)
        assertEquals(UpdateCheck.DOWNLOAD_PAGE, other!!.pageUrl)
        assertEquals(UpdateCheck.DOWNLOAD_PAGE, UpdateCheck.safePage("http://loft.incremental.no/download/"))
        assertEquals(UpdateCheck.DOWNLOAD_PAGE, UpdateCheck.safePage("https://loft.incremental.no.evil.example/"))
        assertEquals(UpdateCheck.DOWNLOAD_PAGE, UpdateCheck.safePage(null))
        assertEquals("https://loft.incremental.no/no/download/", UpdateCheck.safePage("https://loft.incremental.no/no/download/"))
    }
}
