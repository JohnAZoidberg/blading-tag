package app.bladingtag

import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.os.Build
import android.view.Gravity
import android.webkit.ConsoleMessage
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import android.widget.TextView

/** Displays only map tile requests remotely. The route and map code stay on the phone. */
internal class SessionStreetMap(
    context: Context,
    private val route: List<RecordedSession.Point>,
) : FrameLayout(context) {
    private val fallback = SessionRouteView(context, route)
    private val status = TextView(context).apply {
        text = "Loading street map…"
        val padding = (12 * resources.displayMetrics.density).toInt()
        setPadding(padding, padding, padding, padding)
    }
    private var web: WebView? = null

    init {
        addView(fallback, LayoutParams(LayoutParams.MATCH_PARENT, LayoutParams.MATCH_PARENT))
        addView(status, LayoutParams(LayoutParams.WRAP_CONTENT, LayoutParams.WRAP_CONTENT, Gravity.BOTTOM or Gravity.END))
    }

    @SuppressLint("SetJavaScriptEnabled") // Only the packaged map code runs.
    fun load() {
        if (web != null) return
        val assets = context.assets
        fun asset(name: String) = assets.open("session-map/$name").bufferedReader().use { it.readText() }
        var lon = route.first().lon
        val points = route.mapIndexed { i, point ->
            if (i > 0) lon += (point.lon - route[i - 1].lon + 540) % 360 - 180
            "[${point.lat},$lon]"
        }.joinToString(",", "[", "]")
        val html = asset("map.html")
            .replace("{{leaflet-css}}", asset("leaflet.css"))
            .replace("{{leaflet-js}}", asset("leaflet.js"))
            .replace("{{route}}", points)
        val map = WebView(context).apply {
            visibility = INVISIBLE
            contentDescription = "Street map of the recorded GPS route"
            settings.javaScriptEnabled = true
            settings.allowFileAccess = false
            settings.allowContentAccess = false
            settings.userAgentString = "BladingTag/${BuildConfig.VERSION_NAME} (Android ${Build.VERSION.RELEASE}; $SITE)"
            webViewClient = object : WebViewClient() {
                override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                    if (request.isForMainFrame && request.url.toString() == ATTRIBUTION) {
                        runCatching { context.startActivity(Intent(Intent.ACTION_VIEW, request.url)) }
                    }
                    return true
                }
            }
            webChromeClient = object : WebChromeClient() {
                override fun onConsoleMessage(message: ConsoleMessage): Boolean {
                    if (message.message() != "blading-map-tile-loaded") return super.onConsoleMessage(message)
                    visibility = VISIBLE
                    fallback.visibility = GONE
                    status.visibility = GONE
                    return true
                }
            }
        }
        web = map
        addView(map, LayoutParams(LayoutParams.MATCH_PARENT, LayoutParams.MATCH_PARENT))
        map.loadDataWithBaseURL(SITE, html, "text/html", "UTF-8", null)
        postDelayed({
            if (web === map && map.visibility != VISIBLE) status.text = "Street map unavailable"
        }, 8_000)
    }

    fun close() {
        web?.let {
            removeView(it)
            it.destroy()
        }
        web = null
    }

    companion object {
        private const val SITE = "https://github.com/JohnAZoidberg/blading-tag/"
        private const val ATTRIBUTION = "https://www.openstreetmap.org/copyright"
    }
}
