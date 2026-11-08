plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "app.bladingtag"
    compileSdk = 36

    defaultConfig {
        applicationId = "app.bladingtag"
        // 33 for the non-deprecated GATT calls that hand over the value
        // (onCharacteristicChanged(gatt, c, value), writeCharacteristic(c, v, t)).
        minSdk = 33
        targetSdk = 36
        versionCode = 1
        versionName = "0.1.0"
    }

    buildFeatures {
        buildConfig = true
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

dependencies {
    testImplementation("junit:junit:4.13.2")
    // android.jar only has stubs of org.json; the real one for SessionJson tests.
    testImplementation("org.json:json:20250517")
}

kotlin {
    jvmToolchain(17)
    compilerOptions {
        jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
    }
}
