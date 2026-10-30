import java.util.Properties

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.kotlin.serialization)
}

// Distribution: -PloftDistribution=play builds the Google Play version, which never checks
// loft.incremental.no/releases.json for updates (Play updates the app; it does not allow updates from elsewhere).
// The default, direct, is the APK from the website, which keeps the daily check and its off switch.
val loftDistribution = (findProperty("loftDistribution") as String?)?.takeIf { it.isNotBlank() } ?: "direct"
if (loftDistribution !in setOf("direct", "play")) throw GradleException("loftDistribution must be direct or play, not $loftDistribution")

// Release signing, from either
//  - LOFT_SIGNING_PROPERTIES: path of a properties file with storeFile, storePassword, keyAlias, keyPassword, or
//  - Gradle properties LOFT_RELEASE_STORE_FILE, LOFT_RELEASE_STORE_PASSWORD, LOFT_RELEASE_KEY_ALIAS,
//    LOFT_RELEASE_KEY_PASSWORD (e.g. ORG_GRADLE_PROJECT_* in the environment, as the build host's env file sets).
// Without either, release builds are signed with the debug key, which is only good for testing.
val releaseSigning: Properties? = System.getenv("LOFT_SIGNING_PROPERTIES")?.takeIf { it.isNotBlank() }?.let { path ->
    val f = file(path)
    if (!f.isFile) throw GradleException("LOFT_SIGNING_PROPERTIES=$path is not a file")
    Properties().apply { f.inputStream().use { load(it) } }
} ?: (findProperty("LOFT_RELEASE_STORE_FILE") as String?)?.takeIf { it.isNotBlank() }?.let { store ->
    Properties().apply {
        setProperty("storeFile", store)
        for ((k, p) in listOf("storePassword" to "LOFT_RELEASE_STORE_PASSWORD", "keyAlias" to "LOFT_RELEASE_KEY_ALIAS",
                "keyPassword" to "LOFT_RELEASE_KEY_PASSWORD")) {
            setProperty(k, findProperty(p) as String? ?: throw GradleException("$p is not set"))
        }
    }
}

android {
    namespace = "no.incremental.loft"
    compileSdk = 36

    defaultConfig {
        applicationId = "no.incremental.loft"
        minSdk = 26
        targetSdk = 36
        versionCode = 2
        versionName = "0.2.0"
        testInstrumentationRunner = "no.incremental.loft.LoftTestRunner"
        buildConfigField("String", "DISTRIBUTION", "\"$loftDistribution\"")
        manifestPlaceholders["loftDistribution"] = loftDistribution
        // Instrumented tests talk to gateway/scripts/dev_server.py on the host; the runner
        // arguments (loftLan, loftPin) come from the command line, never from the code.
    }

    signingConfigs {
        if (releaseSigning != null) {
            create("release") {
                storeFile = file(releaseSigning.getProperty("storeFile") ?: throw GradleException("storeFile missing"))
                storePassword = releaseSigning.getProperty("storePassword")
                keyAlias = releaseSigning.getProperty("keyAlias")
                keyPassword = releaseSigning.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            signingConfig = if (releaseSigning != null) signingConfigs.getByName("release") else signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlin { jvmToolchain(17) }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    // Android 13+ lists Loft's languages under Settings > Apps > Loft > Language, from the values-* folders
    // (tools/i18n/loft_i18n.py writes them). The default values/ are English (res/resources.properties).
    androidResources {
        generateLocaleConfig = true
    }

    packaging {
        resources.excludes += setOf("/META-INF/{AL2.0,LGPL2.1}", "/META-INF/versions/9/OSGI-INF/MANIFEST.MF")
    }

    testOptions {
        animationsDisabled = true
    }
}

dependencies {
    implementation(project(":core"))
    implementation(libs.kotlinx.coroutines.android)
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.activity.compose)
    implementation(libs.androidx.lifecycle.runtime.compose)
    implementation(libs.androidx.lifecycle.viewmodel.compose)
    implementation(platform(libs.compose.bom))
    implementation(libs.compose.ui)
    implementation(libs.compose.ui.tooling.preview)
    implementation(libs.compose.material3)
    implementation(libs.compose.material.icons)
    implementation(libs.androidx.work.runtime)
    implementation(libs.camerax.camera2)
    implementation(libs.camerax.lifecycle)
    implementation(libs.camerax.view)
    implementation(libs.zxing.core)
    debugImplementation(libs.compose.ui.tooling)
    debugImplementation(libs.compose.ui.test.manifest)

    testImplementation(libs.junit)

    androidTestImplementation(platform(libs.compose.bom))
    androidTestImplementation(libs.compose.ui.test.junit4)
    androidTestImplementation(libs.androidx.test.core)
    androidTestImplementation(libs.androidx.test.runner)
    androidTestImplementation(libs.androidx.test.rules)
    androidTestImplementation(libs.androidx.test.ext.junit)
    androidTestImplementation(libs.kotlinx.coroutines.test)
    androidTestImplementation(libs.androidx.work.testing)
    androidTestImplementation(libs.okhttp.mockwebserver)
    androidTestImplementation(libs.okhttp.tls)
}
