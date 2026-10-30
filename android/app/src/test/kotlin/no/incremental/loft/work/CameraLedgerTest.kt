package no.incremental.loft.work

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class CameraLedgerTest {
    private val h = 3600L

    @Test fun startsAtTheFloor() {
        val l = CameraLedger(1_000_000)
        assertEquals(1_000_000, l.from)
        assertFalse(l.isDone(1))
    }

    @Test fun anOlderPendingItemIsStillLookedAtAfterANewerOneIsDone() {
        val floor = 1_000_000L
        val l = CameraLedger(floor)
        // Video 10 started at floor + 5 (pending); photo 11 at floor + 60 is uploaded first.
        l.mark(11, floor + 60)
        assertTrue("the query still reaches the video's DATE_ADDED", l.from <= floor + 5)
        assertFalse(l.isDone(10))
        l.mark(10, floor + 5)
        assertTrue(l.isDone(10) && l.isDone(11))
    }

    @Test fun theWindowSlidesAndForgetsOldEntries() {
        val floor = 1_000_000L
        val l = CameraLedger(floor)
        l.mark(1, floor + 10)
        l.mark(2, floor + 3 * 24 * h)
        assertEquals(floor + 3 * 24 * h - CameraLedger.LOOKBACK_S, l.from)
        assertFalse("outside the window, and below `from`, so never queried again", l.isDone(1))
        assertEquals(1, l.size)
    }

    @Test fun neverBeforeTheFloor() {
        val l = CameraLedger(5_000_000)
        l.mark(1, 5_000_100)
        assertEquals(5_000_000, l.from)
    }

    @Test fun encodeDecode() {
        val l = CameraLedger(1_000_000)
        l.mark(7, 1_000_007)
        l.mark(8, 1_000_008)
        val back = CameraLedger.decode(1_000_000, l.encode())
        assertTrue(back.isDone(7) && back.isDone(8))
        assertEquals(l.from, back.from)
        assertEquals(0, CameraLedger.decode(1, "garbage,1:x,:,2").size)
        assertEquals(0, CameraLedger.decode(1, null).size)
    }
}
