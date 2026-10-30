// Which camera items are already uploaded. A plain DATE_ADDED watermark is not enough: a video
// gets its DATE_ADDED when recording starts but only shows up (IS_PENDING = 0) when it stops, so a
// photo taken meanwhile would move the watermark past it and the video would never go. So the
// ledger remembers the MediaStore IDs it handled over a look-back window, and every run looks at
// everything added in that window.
package no.incremental.loft.work

class CameraLedger(
    /** DATE_ADDED (seconds) of the moment camera upload was switched on: nothing older is ever looked at. */
    val floor: Long,
    done: Map<Long, Long> = emptyMap(),
) {
    private val done = done.toMutableMap()        // MediaStore _ID -> DATE_ADDED

    /** Newest DATE_ADDED handled so far (or the floor). */
    val newest: Long get() = maxOf(floor, done.values.maxOrNull() ?: floor)

    /** The query starts here: the look-back window before the newest handled item, never before the floor. */
    val from: Long get() = maxOf(floor, newest - LOOKBACK_S)

    fun isDone(id: Long): Boolean = id in done

    fun mark(id: Long, added: Long) {
        done[id] = added
        val cut = newest - LOOKBACK_S
        done.values.removeAll { it < cut }
    }

    val size: Int get() = done.size

    /** `id:added,id:added,...` for SharedPreferences. */
    fun encode(): String = done.entries.joinToString(",") { "${it.key}:${it.value}" }

    companion object {
        /** A recording (or any pending item) that takes longer than this to finish is not uploaded. */
        const val LOOKBACK_S = 48 * 3600L

        fun decode(floor: Long, s: String?): CameraLedger = CameraLedger(floor,
            s.orEmpty().split(',').mapNotNull { e ->
                val (id, added) = e.split(':').takeIf { it.size == 2 } ?: return@mapNotNull null
                (id.toLongOrNull() ?: return@mapNotNull null) to (added.toLongOrNull() ?: return@mapNotNull null)
            }.toMap())
    }
}
