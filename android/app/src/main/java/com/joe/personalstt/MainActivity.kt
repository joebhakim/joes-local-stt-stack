package com.joe.personalstt

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.SharedPreferences
import android.content.pm.PackageManager
import android.os.Bundle
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.view.Gravity
import android.view.MotionEvent
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import org.json.JSONObject
import java.util.Locale
import java.util.UUID

class MainActivity : Activity(), RecognitionListener {
    private lateinit var prefs: SharedPreferences
    private lateinit var urlField: EditText
    private lateinit var tokenField: EditText
    private lateinit var statusText: TextView
    private lateinit var partialText: TextView
    private lateinit var finalText: TextView
    private lateinit var connectButton: Button
    private lateinit var holdButton: Button
    private lateinit var startButton: Button
    private lateinit var stopButton: Button
    private lateinit var cancelButton: Button

    private val httpClient = OkHttpClient()
    private var socket: WebSocket? = null
    private var recognizer: SpeechRecognizer? = null
    private var connected = false
    private var authenticated = false
    private var sessionActive = false
    private var sessionId = ""
    private var seq = 0

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = getSharedPreferences("personal_stt", MODE_PRIVATE)
        buildUi()
        loadSettings()
        ensureAudioPermission()
        updateButtons()
    }

    override fun onDestroy() {
        recognizer?.destroy()
        socket?.close(1000, "activity destroyed")
        httpClient.dispatcher.executorService.shutdown()
        super.onDestroy()
    }

    private fun buildUi() {
        val outer = LinearLayout(this)
        outer.orientation = LinearLayout.VERTICAL
        outer.setPadding(28, 28, 28, 28)

        val title = TextView(this)
        title.text = "Personal STT"
        title.textSize = 24f
        title.setTextColor(0xff111827.toInt())
        outer.addView(title)

        urlField = EditText(this)
        urlField.hint = "ws://127.0.0.1:8765/stt"
        outer.addView(urlField)

        tokenField = EditText(this)
        tokenField.hint = "bridge PIN"
        tokenField.inputType = android.text.InputType.TYPE_CLASS_NUMBER
        tokenField.setSingleLine(true)
        outer.addView(tokenField)

        val presets = LinearLayout(this)
        presets.orientation = LinearLayout.HORIZONTAL
        val usbButton = Button(this)
        usbButton.text = "USB"
        usbButton.setOnClickListener {
            urlField.setText("ws://127.0.0.1:8765/stt")
            saveSettings()
        }
        val wifiButton = Button(this)
        wifiButton.text = "Wi-Fi"
        wifiButton.setOnClickListener {
            urlField.setText("ws://<desktop-ip>:8765/stt")
            saveSettings()
        }
        presets.addView(usbButton, rowWeight())
        presets.addView(wifiButton, rowWeight())
        outer.addView(presets)

        connectButton = Button(this)
        connectButton.text = "Connect"
        connectButton.setOnClickListener {
            if (connected) {
                disconnect()
            } else {
                connect()
            }
        }
        outer.addView(connectButton)

        holdButton = Button(this)
        holdButton.text = "Hold To Talk"
        holdButton.setOnTouchListener { _, event ->
            when (event.action) {
                MotionEvent.ACTION_DOWN -> {
                    startDictation()
                    true
                }
                MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                    stopDictation()
                    true
                }
                else -> false
            }
        }
        outer.addView(holdButton)

        val controls = LinearLayout(this)
        controls.orientation = LinearLayout.HORIZONTAL
        startButton = Button(this)
        startButton.text = "Start"
        startButton.setOnClickListener { startDictation() }
        stopButton = Button(this)
        stopButton.text = "Stop"
        stopButton.setOnClickListener { stopDictation() }
        cancelButton = Button(this)
        cancelButton.text = "Cancel"
        cancelButton.setOnClickListener { cancelDictation() }
        controls.addView(startButton, rowWeight())
        controls.addView(stopButton, rowWeight())
        controls.addView(cancelButton, rowWeight())
        outer.addView(controls)

        statusText = TextView(this)
        statusText.text = "Disconnected"
        statusText.gravity = Gravity.START
        outer.addView(statusText)

        partialText = TextView(this)
        partialText.textSize = 18f
        partialText.setTextColor(0xffb45309.toInt())
        finalText = TextView(this)
        finalText.textSize = 18f
        finalText.setTextColor(0xff064e3b.toInt())

        val scroll = ScrollView(this)
        val transcript = LinearLayout(this)
        transcript.orientation = LinearLayout.VERTICAL
        transcript.addView(sectionLabel("Partial"))
        transcript.addView(partialText)
        transcript.addView(sectionLabel("Final"))
        transcript.addView(finalText)
        scroll.addView(transcript)
        outer.addView(scroll, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT,
            0,
            1f,
        ))

        setContentView(outer)
    }

    private fun sectionLabel(text: String): TextView {
        val label = TextView(this)
        label.text = text.uppercase(Locale.US)
        label.textSize = 12f
        label.setTextColor(0xff6b7280.toInt())
        return label
    }

    private fun rowWeight(): LinearLayout.LayoutParams {
        return LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
    }

    private fun loadSettings() {
        urlField.setText(prefs.getString("url", "ws://127.0.0.1:8765/stt"))
        tokenField.setText(prefs.getString("token", ""))
    }

    private fun saveSettings() {
        prefs.edit()
            .putString("url", urlField.text.toString().trim())
            .putString("token", tokenField.text.toString().trim())
            .apply()
    }

    private fun ensureAudioPermission() {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), 10)
        }
    }

    private fun connect() {
        saveSettings()
        val request = Request.Builder().url(urlField.text.toString().trim()).build()
        socket = httpClient.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                connected = true
                val hello = JSONObject()
                    .put("type", "hello")
                    .put("token", tokenField.text.toString().trim())
                webSocket.send(hello.toString())
                runOnUiThread {
                    statusText.text = "Connected, authenticating"
                    updateButtons()
                }
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                val ok = runCatching { JSONObject(text).optBoolean("ok", false) }.getOrDefault(false)
                if (ok) {
                    authenticated = true
                }
                runOnUiThread {
                    statusText.text = if (ok) "Connected" else text
                    updateButtons()
                }
            }

            override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
                onMessage(webSocket, bytes.utf8())
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                connected = false
                authenticated = false
                runOnUiThread {
                    statusText.text = "Connection failed: ${t.message}"
                    updateButtons()
                }
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                connected = false
                authenticated = false
                runOnUiThread {
                    statusText.text = "Disconnected"
                    updateButtons()
                }
            }
        })
    }

    private fun disconnect() {
        socket?.close(1000, "user disconnect")
        socket = null
        connected = false
        authenticated = false
        updateButtons()
    }

    private fun startDictation() {
        if (!authenticated || sessionActive) {
            return
        }
        ensureAudioPermission()
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            statusText.text = "Microphone permission required"
            return
        }
        sessionId = "android-${UUID.randomUUID()}"
        seq = 0
        partialText.text = ""
        finalText.text = ""
        sessionActive = true
        sendEvent("start", "")
        recognizer?.destroy()
        recognizer = SpeechRecognizer.createSpeechRecognizer(this)
        recognizer?.setRecognitionListener(this)
        recognizer?.startListening(recognizerIntent())
        statusText.text = "Listening"
        updateButtons()
    }

    private fun stopDictation() {
        if (!sessionActive) {
            return
        }
        recognizer?.stopListening()
        statusText.text = "Finalizing"
        updateButtons()
    }

    private fun cancelDictation() {
        if (!sessionActive) {
            return
        }
        recognizer?.cancel()
        sendEvent("cancel", "")
        sessionActive = false
        partialText.text = ""
        statusText.text = "Cancelled"
        updateButtons()
    }

    private fun recognizerIntent(): Intent {
        return Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
            putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
            putExtra(RecognizerIntent.EXTRA_LANGUAGE, Locale.getDefault())
        }
    }

    private fun sendEvent(type: String, text: String, error: String = "") {
        val payload = JSONObject()
            .put("type", type)
            .put("session_id", sessionId)
            .put("source", "android")
            .put("seq", ++seq)
        if (text.isNotBlank()) {
            payload.put("text", text)
        }
        if (error.isNotBlank()) {
            payload.put("error", error)
        }
        socket?.send(payload.toString())
    }

    private fun bestResult(results: Bundle?): String {
        val items = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
        return items?.firstOrNull().orEmpty()
    }

    private fun updateButtons() {
        connectButton.text = if (connected) "Disconnect" else "Connect"
        holdButton.isEnabled = authenticated
        startButton.isEnabled = authenticated && !sessionActive
        stopButton.isEnabled = authenticated && sessionActive
        cancelButton.isEnabled = authenticated && sessionActive
    }

    override fun onReadyForSpeech(params: Bundle?) {
        statusText.text = "Ready"
    }

    override fun onBeginningOfSpeech() {
        statusText.text = "Listening"
    }

    override fun onRmsChanged(rmsdB: Float) {}

    override fun onBufferReceived(buffer: ByteArray?) {}

    override fun onEndOfSpeech() {
        statusText.text = "Finalizing"
    }

    override fun onError(error: Int) {
        if (sessionActive) {
            sendEvent("error", "", "SpeechRecognizer error $error")
            sessionActive = false
        }
        statusText.text = "Recognizer error $error"
        updateButtons()
    }

    override fun onResults(results: Bundle?) {
        val text = bestResult(results)
        finalText.text = text
        if (sessionActive) {
            sendEvent("final", text)
        }
        sessionActive = false
        statusText.text = "Final sent"
        updateButtons()
    }

    override fun onPartialResults(partialResults: Bundle?) {
        val text = bestResult(partialResults)
        partialText.text = text
        if (sessionActive && text.isNotBlank()) {
            sendEvent("partial", text)
        }
    }

    override fun onEvent(eventType: Int, params: Bundle?) {}
}
