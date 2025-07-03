import java.security.MessageDigest
import java.util.*
import javax.crypto.Cipher
import javax.crypto.spec.SecretKeySpec
import kotlin.collections.LinkedHashMap

class User(
    val nin: String,
    private var encryptedPhone: ByteArray,
    var isBiometricEnabled: Boolean = false,
    private var passwordHash: String? = null,
    private val activityLog: MutableList<String> = mutableListOf()
) {
    companion object {
        private const val SECRET_KEY = "NIRA_System_Secret"
        private val keySpec = SecretKeySpec(SECRET_KEY.toByteArray().copyOf(16), "AES")
        
        fun encrypt(data: String): ByteArray {
            val cipher = Cipher.getInstance("AES/ECB/PKCS5Padding")
            cipher.init(Cipher.ENCRYPT_MODE, keySpec)
            return cipher.doFinal(data.toByteArray())
        }
        
        fun decrypt(data: ByteArray): String {
            val cipher = Cipher.getInstance("AES/ECB/PKCS5Padding")
            cipher.init(Cipher.DECRYPT_MODE, keySpec)
            return String(cipher.doFinal(data))
        }
    }
    
    val phoneNumber: String
        get() = decrypt(encryptedPhone)
    
    fun addLogEntry(entry: String) {
        activityLog.add("${Date()} - $entry")
        if (activityLog.size > 20) activityLog.removeAt(0)
    }
    
    fun getActivityLog(): List<String> = activityLog.toList()
    
    fun setPassword(password: String) {
        passwordHash = hashPassword(password)
    }
    
    fun verifyPassword(password: String): Boolean {
        return passwordHash == hashPassword(password)
    }
    
    private fun hashPassword(password: String): String {
        return MessageDigest.getInstance("SHA-256")
            .digest(password.toByteArray())
            .joinToString("") { "%02x".format(it) }
    }
}

class Home {
    private val context = this
    private val activity = this
    private val registeredUsers = mutableMapOf<String, User>()
    private var login_nin: String? = null
    private var currentUser: User? = null
    private val failedAttempts = mutableMapOf<String, Int>()
    private val blockedUsers = mutableSetOf<String>()
    private val adminPassword = "Admin@NIRA123".hashCode().toString()
    private val systemLog = LinkedList<String>()

    init {
        systemLog.add("System initialized at ${Date()}")
    }

    private fun sendauthcode(phoneNumber: String): String {
        val authCode = (100000..999999).random().toString()
        println("Simulated SMS sent to $phoneNumber: Your authentication code is $authCode")
        return authCode
    }

    private fun canUseBiometrics(): Boolean {
        println("Checking biometric capabilities...")
        return true
    }

    private fun authenticateWithBiometrics(onSuccess: () -> Unit, onFailure: () -> Unit) {
        println("\n===== BIOMETRIC AUTHENTICATION =====")
        println("Instructions:")
        println("1. For fingerprint: Place finger flat on sensor")
        println("2. For face recognition: Look directly at camera")
        println("3. For iris scan: Position eyes within frame\n")
        
        println("Simulating biometric authentication...")
        print("Enter result (success/fail): ")
        if (readln().equals("success", ignoreCase = true)) {
            onSuccess()
        } else {
            onFailure()
        }
    }

    fun start() {
        while (true) {
            println("\n===== NIRA NATIONAL IDENTITY SYSTEM =====")
            println("1. New Registration")
            println("2. Login")
            println("3. Admin Portal")
            println("4. Help")
            println("5. Exit")
            
            when (readln().toIntOrNull() ?: -1) {
                1 -> newRegistration()
                2 -> login()
                3 -> adminPortal()
                4 -> help()
                5 -> {
                    println("System shutting down...")
                    return
                }
                else -> println("Invalid selection")
            }
        }
    }

    private fun newRegistration() {
        println("\n===== NEW REGISTRATION =====")
        val nin = getInput("Enter NIN (14 digits)", 14, 14, "NIN must be 14 digits")
        if (registeredUsers.containsKey(nin)) {
            println("NIN already registered")
            return
        }

        val phone = getInput("Enter phone number (10 digits)", 10, 10, "Invalid phone number")
        val password = getPassword()
        
        val user = User(
            nin = nin,
            encryptedPhone = User.encrypt(phone),
            passwordHash = hashPassword(password)
        )
        
        registeredUsers[nin] = user
        login_nin = nin
        
        println("\nSending verification code...")
        val authCode = sendauthcode(phone)
        
        if (verifyCode(authCode)) {
            user.addLogEntry("Registration completed")
            systemLog.add("New user registered: $nin")
            
            if (canUseBiometrics() && getYesNo("Enable biometric login?")) {
                user.isBiometricEnabled = true
                println("Biometrics enabled")
            }
            
            println("""
            |Registration successful!
            |=======================
            |NIN: $nin
            |Password: ${"*".repeat(password.length)}
            |Biometrics: ${if(user.isBiometricEnabled) "Enabled" else "Disabled"}
            """.trimMargin())
        } else {
            registeredUsers.remove(nin)
            println("Registration failed")
        }
    }

    private fun login() {
        if (currentUser != null) {
            println("Already logged in as ${currentUser?.nin}")
            return
        }
        
        println("\n===== USER LOGIN =====")
        val nin = getInput("Enter NIN", 14, 14, "Invalid NIN")
        
        if (blockedUsers.contains(nin)) {
            println("Account blocked. Contact support.")
            return
        }
        
        val user = registeredUsers[nin] ?: run {
            println("User not found")
            return
        }
        
        // Track failed attempts
        val attempts = failedAttempts.getOrDefault(nin, 0)
        if (attempts >= 2) {
            println("Account locked. Too many failed attempts.")
            blockedUsers.add(nin)
            return
        }
        
        // Biometric login attempt
        if (user.isBiometricEnabled && canUseBiometrics() && getYesNo("Use biometric login?")) {
            authenticateWithBiometrics(
                onSuccess = {
                    currentUser = user
                    user.addLogEntry("Biometric login successful")
                    println("Login successful! Welcome ${user.nin}")
                    userMenu()
                },
                onFailure = {
                    user.addLogEntry("Biometric login failed")
                    println("Biometric authentication failed")
                    passwordLogin(user)
                }
            )
        } else {
            passwordLogin(user)
        }
    }
    
    private fun passwordLogin(user: User) {
        val password = getInput("Enter password", minLen = 6)
        if (user.verifyPassword(password)) {
            currentUser = user
            failedAttempts.remove(user.nin)
            user.addLogEntry("Password login successful")
            println("Login successful! Welcome ${user.nin}")
            userMenu()
        } else {
            val attempts = failedAttempts.getOrDefault(user.nin, 0) + 1
            failedAttempts[user.nin] = attempts
            user.addLogEntry("Failed login attempt ($attempts/3)")
            println("Invalid password. Attempts remaining: ${3 - attempts}")
        }
    }

    private fun userMenu() {
        while (currentUser != null) {
            val user = currentUser!!
            println("\n===== USER DASHBOARD =====")
            println("Logged in as: ${user.nin}")
            println("1. Renew ID")
            println("2. Report Lost ID")
            println("3. View Activity Log")
            println("4. Change Password")
            println("5. Toggle Biometrics")
            println("6. Logout")
            
            when (readln().toIntOrNull() ?: -1) {
                1 -> renewId(user)
                2 -> reportLostId(user)
                3 -> viewActivityLog(user)
                4 -> changePassword(user)
                5 -> toggleBiometrics(user)
                6 -> {
                    user.addLogEntry("User logged out")
                    currentUser = null
                    println("Logged out successfully")
                    return
                }
                else -> println("Invalid selection")
            }
        }
    }

    private fun renewId(user: User) {
        println("\n===== ID RENEWAL =====")
        sendVerification(user, "renewal")
        user.addLogEntry("ID renewal requested")
        println("Renewal request submitted successfully")
    }

    private fun reportLostId(user: User) {
        println("\n===== LOST ID REPORT =====")
        sendVerification(user, "replacement")
        user.addLogEntry("Reported lost ID")
        println("Replacement request submitted successfully")
    }

    private fun sendVerification(user: User, operation: String) {
        println("Sending verification code to ${user.phoneNumber}...")
        val authCode = sendauthcode(user.phoneNumber)
        if (!verifyCode(authCode)) {
            throw SecurityException("Verification failed for $operation")
        }
    }

    private fun viewActivityLog(user: User) {
        println("\n===== ACTIVITY LOG =====")
        user.getActivityLog().forEachIndexed { i, entry ->
            println("${i + 1}. $entry")
        }
        user.addLogEntry("Viewed activity log")
    }

    private fun changePassword(user: User) {
        println("\n===== PASSWORD CHANGE =====")
        val current = getInput("Enter current password", minLen = 6)
        if (!user.verifyPassword(current)) {
            println("Incorrect password")
            return
        }
        
        val newPass = getPassword()
        user.setPassword(newPass)
        user.addLogEntry("Password changed")
        println("Password updated successfully")
    }

    private fun toggleBiometrics(user: User) {
        println("\n===== BIOMETRIC SETTINGS =====")
        if (user.isBiometricEnabled) {
            if (getYesNo("Disable biometric authentication?")) {
                user.isBiometricEnabled = false
                user.addLogEntry("Biometrics disabled")
                println("Biometrics disabled")
            }
        } else {
            if (canUseBiometrics() && getYesNo("Enable biometric authentication?")) {
                authenticateWithBiometrics(
                    onSuccess = {
                        user.isBiometricEnabled = true
                        user.addLogEntry("Biometrics enabled")
                        println("Biometrics enabled")
                    },
                    onFailure = {
                        println("Biometric setup failed")
                    }
                )
            }
        }
    }

    private fun adminPortal() {
        println("\n===== ADMIN LOGIN =====")
        val password = getInput("Enter admin password", minLen = 8)
        if (hashPassword(password) != adminPassword) {
            println("Invalid admin credentials")
            return
        }
        
        println("\n===== ADMIN DASHBOARD =====")
        println("Registered users: ${registeredUsers.size}")
        println("Blocked accounts: ${blockedUsers.size}")
        println("System log entries: ${systemLog.size}")
        
        while (true) {
            println("\n1. View system logs")
            println("2. Unblock account")
            println("3. View user details")
            println("4. Exit admin mode")
            
            when (readln().toIntOrNull() ?: -1) {
                1 -> viewSystemLogs()
                2 -> unblockAccount()
                3 -> viewUserDetails()
                4 -> return
                else -> println("Invalid selection")
            }
        }
    }

    private fun viewSystemLogs() {
        println("\n===== SYSTEM LOGS =====")
        systemLog.forEachIndexed { i, entry -> 
            println("${i + 1}. $entry") 
        }
    }

    private fun unblockAccount() {
        val nin = getInput("Enter NIN to unblock", 14, 14)
        if (blockedUsers.remove(nin)) {
            failedAttempts.remove(nin)
            systemLog.add("Admin unblocked account: $nin")
            println("Account $nin unblocked")
        } else {
            println("Account not blocked or invalid NIN")
        }
    }

    private fun viewUserDetails() {
        val nin = getInput("Enter user NIN", 14, 14)
        val user = registeredUsers[nin] ?: run {
            println("User not found")
            return
        }
        
        println("\n===== USER DETAILS =====")
        println("NIN: ${user.nin}")
        println("Phone: ${user.phoneNumber}")
        println("Biometrics: ${if (user.isBiometricEnabled) "Enabled" else "Disabled"}")
        println("Last activities:")
        user.getActivityLog().takeLast(5).forEach(::println)
    }

    private fun help() {
        println("\n===== HELP CENTER =====")
        println("Type your question or 'menu' to return")
        
        val helpResponses = mapOf(
            "register" to "New registration requires your NIN and phone number",
            "login" to "Use your NIN and password to access your account",
            "renew" to "ID renewal is available after login under Renew ID",
            "lost" to "Report lost IDs through the Lost ID option after login",
            "password" to "Change password in your account settings",
            "biometric" to "Manage biometric settings in your account",
            "admin" to "Admin portal is for system administrators only"
        )
        
        while (true) {
            print("\nHelp:> ")
            val query = readln().lowercase()
            if (query == "menu") return
            
            val response = helpResponses.entries.find { query.contains(it.key) }?.value
                ?: "I'm sorry, I couldn't understand your query. Try: register, login, renew, lost, password, biometric"
            
            println(response)
        }
    }

    // Utility functions
    private fun getInput(prompt: String, minLen: Int = 1, maxLen: Int = 50, errorMsg: String = "Invalid input"): String {
        while (true) {
            println(prompt)
            val input = readln().trim()
            if (input.length in minLen..maxLen) return input
            println("$errorMsg (${input.length} chars)")
        }
    }
    
    private fun getPassword(): String {
        while (true) {
            val pass1 = getInput("Create password (min 8 chars)", 8, 32)
            val pass2 = getInput("Confirm password", 8, 32)
            if (pass1 == pass2) return pass1
            println("Passwords don't match")
        }
    }
    
    private fun verifyCode(validCode: String): Boolean {
        val userCode = getInput("Enter verification code", 6, 6, "Code must be 6 digits")
        return userCode == validCode
    }
    
    private fun getYesNo(prompt: String): Boolean {
        println("$prompt (yes/no)")
        return readln().equals("yes", ignoreCase = true)
    }
    
    private fun hashPassword(password: String): String {
        return MessageDigest.getInstance("SHA-256")
            .digest(password.toByteArray())
            .joinToString("") { "%02x".format(it) }
    }
}

fun main() {
    Home().start()
}