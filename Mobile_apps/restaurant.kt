class Menu {
    fun displayIntro() {
        println("WELCOME, WHAT MENU WOULD YOU LIKE?")
        println(
            """ 1. Main course 
                2. Snacks
                3. Drinks """
        )
    }

    fun chooser() {
        val choice = readln().toInt()
        when (choice) {
            1 -> {
                val option = println(
                    """Welcome to the Main Course menu
                        |Welcome to Our main courses
                        |1.Beef - 5000
                        |2.Peas - 4000
                        |3.Liver - 6000
                        |4.Beans - 3000
                        |5.Gravy - 3500
                    """.trimMargin()
                )
                println("Enter option: ")
                val optionInt = readln().toInt()
                when (optionInt) {
                    1 -> println("Your Beef order is coming right away!")
                    2 -> println("Your Peas order is coming right away!")
                    3 -> println("Your Liver order is coming right away!")
                    4 -> println("Your Beans order is coming right away!")
                    5 -> println("Your Gravy order is coming right away!")
                    else -> println("Invalid choice, please select a number between 1 and 5.")
                }
            }
            2 -> {
                println(
                    """Welcome to our Snacks menu
                        |1.Samosa - 1000 @
                        |2.Chapati - 1000 @
                        |3.Kebab - 1500 @
                        |4.Meat pie - 1500 @
                    """.trimMargin()
                )
                println("Enter option: ")
                val option2 = readln().toInt()
                when (option2) {
                    1 -> {println("How many samosas would you like?")
                          val qty = readln().toInt()
                          val total_price = (qty*1000)
                           println("""Your order of $qty is coming right away!
                                      |Total price is $total_price    
                           """.trimMargin())
                    }
                    2 -> {println("How many chapatis would you like?")
                    val chapqty = readln().toInt()
                    val total_chap = (chapqty*1000)
                    println("""Your order of $chapqty chaptis is coming right away!
                               |Total price is $total_chap
                    """.trimMargin())}
                    3 -> {println("How many kebabs would you like?")
                    val kebqty = readln().toInt()
                    val total_keb = (kebqty*1500)
                    println("""Your order of $kebqty kebabs is coming right away!
                               |Total price is $total_keb
                    """.trimMargin())}
                    4 -> {println("How many meat pies would you like?")
                    val meatqty = readln().toInt()
                    val total_meat = (meatqty*1500)
                    println("""Your order of $meatqty meat pies is coming right away!
                               |Total price is $total_meat
                    """.trimMargin())}
                    else -> println("Invalid choice, please select a number between 1 and 4.")
                }
            }
            3 -> {
                println(
                    """Welcome to our Drinks menu
                        |1.Soda - 1500
                        |2.Cocktail Juice - 1500
                        |3.Mocktail - 16000
                        |4.Yoghurt - 8000
                    """.trimMargin()
                )
                println("Enter option: ")
                val drinkmenu = readln().toInt()
                when (drinkmenu) {
                    1 -> {println("How many sodas would you like?")
                    val sodaqty = readln().toInt()
                    val total_soda = (sodaqty*1500)
                    println("""Your order of $sodaqty sodas is coming right away!
                               |Total price is $total_soda
                    """.trimMargin())}
                    2 -> {println("How many cocktail juices would you like?")
                    val juiceqty = readln().toInt()
                    val total_juice = (juiceqty*1500)
                    println("""Your order of $juiceqty cocktail juices is coming right away!
                               |Total price is $total_juice
                    """.trimMargin())}
                    3 -> {println("How many mocktails would you like?")
                    val mockqty = readln().toInt()
                    val total_mock = (mockqty*1600)
                    println("""Your order of $mockqty mocktails is coming right away!
                               |Total price is $total_mock
                    """.trimMargin())}
                    4 -> {println("How many yoghurts would you like?")
                    val yogqty = readln().toInt()
                    val total_yog = (yogqty*800)
                    println("""Your order of $yogqty yoghurts is coming right away!
                               |Total price is $total_yog
                    """.trimMargin())}
                    else -> println("Invalid choice, please select a number between 1 and 4.")
                }
            }
            else -> {
                println("Invalid choice")
            }
        }
    }
}

fun main() {
    val menu = Menu()
    menu.displayIntro()
    menu.chooser()
}