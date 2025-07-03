import itertools
import string

# Define character sets
lowercase = string.ascii_lowercase  # a-z
uppercase = string.ascii_uppercase  # A-Z
digits = string.digits  # 0-9
special_chars = "!@#$%^&*()_+-=[]{}|;:,.<>?"  # Common special characters
charset = lowercase + uppercase + digits + special_chars  # Full character set

# Parameters
min_length = 12
max_length = 12  # Keep small to avoid huge output
output_file = "wordlist.txt"

# Generate and save wordlist
with open(output_file, 'w') as f:
    for length in range(min_length, max_length + 1):
        # Generate all combinations for the current length
        for combo in itertools.product(charset, repeat=length):
            word = ''.join(combo)
            f.write(word + '\n')

print(f"Wordlist saved to {output_file}")