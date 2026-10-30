# kotlinx.serialization: keep the generated serializers of our wire models.
-keepattributes *Annotation*, InnerClasses
-dontnote kotlinx.serialization.**
-keepclassmembers @kotlinx.serialization.Serializable class no.incremental.loft.** {
    *** Companion;
    kotlinx.serialization.KSerializer serializer(...);
}
-keepclasseswithmembers class no.incremental.loft.** {
    kotlinx.serialization.KSerializer serializer(...);
}
-keep,includedescriptorclasses class no.incremental.loft.**$$serializer { *; }

# ZXing is used reflection-free; nothing to keep. OkHttp ships its own rules.
-dontwarn org.bouncycastle.**
-dontwarn org.conscrypt.**
-dontwarn org.openjsse.**
