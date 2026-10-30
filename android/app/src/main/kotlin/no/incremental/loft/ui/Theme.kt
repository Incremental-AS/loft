// Material 3 with the Loft scheme (seed loft rust #B5541A, scheme Fidelity), read from
// res/values/loft_m3_colors.xml exactly as exported. Dynamic colour stays off so Loft
// looks like Loft; light/dark follows the system.
package no.incremental.loft.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.res.colorResource
import no.incremental.loft.R

@Composable
fun LoftTheme(content: @Composable () -> Unit) {
    val scheme = if (isSystemInDarkTheme()) {
        darkColorScheme(
            primary = colorResource(R.color.md_theme_dark_primary),
            onPrimary = colorResource(R.color.md_theme_dark_on_primary),
            primaryContainer = colorResource(R.color.md_theme_dark_primary_container),
            onPrimaryContainer = colorResource(R.color.md_theme_dark_on_primary_container),
            secondary = colorResource(R.color.md_theme_dark_secondary),
            onSecondary = colorResource(R.color.md_theme_dark_on_secondary),
            secondaryContainer = colorResource(R.color.md_theme_dark_secondary_container),
            onSecondaryContainer = colorResource(R.color.md_theme_dark_on_secondary_container),
            tertiary = colorResource(R.color.md_theme_dark_tertiary),
            onTertiary = colorResource(R.color.md_theme_dark_on_tertiary),
            tertiaryContainer = colorResource(R.color.md_theme_dark_tertiary_container),
            onTertiaryContainer = colorResource(R.color.md_theme_dark_on_tertiary_container),
            error = colorResource(R.color.md_theme_dark_error),
            onError = colorResource(R.color.md_theme_dark_on_error),
            errorContainer = colorResource(R.color.md_theme_dark_error_container),
            onErrorContainer = colorResource(R.color.md_theme_dark_on_error_container),
            background = colorResource(R.color.md_theme_dark_surface),
            onBackground = colorResource(R.color.md_theme_dark_on_surface),
            surface = colorResource(R.color.md_theme_dark_surface),
            onSurface = colorResource(R.color.md_theme_dark_on_surface),
            surfaceVariant = colorResource(R.color.md_theme_dark_surface_variant),
            onSurfaceVariant = colorResource(R.color.md_theme_dark_on_surface_variant),
            surfaceContainerLowest = colorResource(R.color.md_theme_dark_surface_container_lowest),
            surfaceContainerLow = colorResource(R.color.md_theme_dark_surface_container_low),
            surfaceContainer = colorResource(R.color.md_theme_dark_surface_container),
            surfaceContainerHigh = colorResource(R.color.md_theme_dark_surface_container_high),
            surfaceContainerHighest = colorResource(R.color.md_theme_dark_surface_container_highest),
            outline = colorResource(R.color.md_theme_dark_outline),
            outlineVariant = colorResource(R.color.md_theme_dark_outline_variant),
            inverseSurface = colorResource(R.color.md_theme_dark_inverse_surface),
            inverseOnSurface = colorResource(R.color.md_theme_dark_inverse_on_surface),
            inversePrimary = colorResource(R.color.md_theme_dark_inverse_primary),
        )
    } else {
        lightColorScheme(
            primary = colorResource(R.color.md_theme_light_primary),
            onPrimary = colorResource(R.color.md_theme_light_on_primary),
            primaryContainer = colorResource(R.color.md_theme_light_primary_container),
            onPrimaryContainer = colorResource(R.color.md_theme_light_on_primary_container),
            secondary = colorResource(R.color.md_theme_light_secondary),
            onSecondary = colorResource(R.color.md_theme_light_on_secondary),
            secondaryContainer = colorResource(R.color.md_theme_light_secondary_container),
            onSecondaryContainer = colorResource(R.color.md_theme_light_on_secondary_container),
            tertiary = colorResource(R.color.md_theme_light_tertiary),
            onTertiary = colorResource(R.color.md_theme_light_on_tertiary),
            tertiaryContainer = colorResource(R.color.md_theme_light_tertiary_container),
            onTertiaryContainer = colorResource(R.color.md_theme_light_on_tertiary_container),
            error = colorResource(R.color.md_theme_light_error),
            onError = colorResource(R.color.md_theme_light_on_error),
            errorContainer = colorResource(R.color.md_theme_light_error_container),
            onErrorContainer = colorResource(R.color.md_theme_light_on_error_container),
            background = colorResource(R.color.md_theme_light_surface),
            onBackground = colorResource(R.color.md_theme_light_on_surface),
            surface = colorResource(R.color.md_theme_light_surface),
            onSurface = colorResource(R.color.md_theme_light_on_surface),
            surfaceVariant = colorResource(R.color.md_theme_light_surface_variant),
            onSurfaceVariant = colorResource(R.color.md_theme_light_on_surface_variant),
            surfaceContainerLowest = colorResource(R.color.md_theme_light_surface_container_lowest),
            surfaceContainerLow = colorResource(R.color.md_theme_light_surface_container_low),
            surfaceContainer = colorResource(R.color.md_theme_light_surface_container),
            surfaceContainerHigh = colorResource(R.color.md_theme_light_surface_container_high),
            surfaceContainerHighest = colorResource(R.color.md_theme_light_surface_container_highest),
            outline = colorResource(R.color.md_theme_light_outline),
            outlineVariant = colorResource(R.color.md_theme_light_outline_variant),
            inverseSurface = colorResource(R.color.md_theme_light_inverse_surface),
            inverseOnSurface = colorResource(R.color.md_theme_light_inverse_on_surface),
            inversePrimary = colorResource(R.color.md_theme_light_inverse_primary),
        )
    }
    MaterialTheme(colorScheme = scheme, content = content)
}
