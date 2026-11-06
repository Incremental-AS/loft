package no.incremental.loft.ui

import java.util.Locale
import org.junit.Assert.assertEquals
import org.junit.Test

class GatewayGuideUrlTest {
    private fun url(tag: String) = gatewayGuideUrl(Locale.forLanguageTag(tag))

    @Test fun languagesMapToTheWebsitePrefixes() {
        assertEquals("https://loft.incremental.no/gateway/", url("en-GB"))
        assertEquals("https://loft.incremental.no/no/gateway/", url("nb-NO"))
        assertEquals("https://loft.incremental.no/no/gateway/", url("nn"))
        assertEquals("https://loft.incremental.no/de/gateway/", url("de-AT"))
        assertEquals("https://loft.incremental.no/pt/gateway/", url("pt-BR"))
        assertEquals("https://loft.incremental.no/zh/gateway/", url("zh-Hans-CN"))
        assertEquals("https://loft.incremental.no/zh/gateway/", url("zh-CN"))
        assertEquals("https://loft.incremental.no/gateway/", url("zh-Hant-TW"))
        assertEquals("https://loft.incremental.no/gateway/", url("sv-SE"))
    }
}
