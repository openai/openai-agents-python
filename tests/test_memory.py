import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
import sys

# Ensure src directory is in Python path
sys.path.append(str(Path(__file__).resolve().parents[1]))

from src.agents import Agent, Runner
from src.agents.memory import AgentMemory, InMemoryMemory, FileStorageMemory

class TestInMemoryMemory(unittest.TestCase):
    def setUp(self):
        self.memory = InMemoryMemory()

    def test_add_and_get_messages(self):
        self.assertEqual(self.memory.get_messages(), [])
        self.memory.add(role="user", content="Hello")
        self.assertEqual(len(self.memory.get_messages()), 1)
        self.assertEqual(self.memory.get_messages()[0], {"role": "user", "content": "Hello"})

        self.memory.add(role="assistant", content="Hi there!", metadata={"timestamp": 123})
        self.assertEqual(len(self.memory.get_messages()), 2)
        self.assertEqual(
            self.memory.get_messages()[1],
            {"role": "assistant", "content": "Hi there!", "metadata": {"timestamp": 123}},
        )
        # Ensure original list is returned, not a copy that can be mutated externally for this test
        messages = self.memory.get_messages()
        messages.append({"role": "system", "content": "mutated"})
        self.assertEqual(len(self.memory.get_messages()), 2) # Should not reflect the append

    def test_get_last_n_messages(self):
        self.memory.add(role="user", content="Message 1")
        self.memory.add(role="assistant", content="Message 2")
        self.memory.add(role="user", content="Message 3")
        self.memory.add(role="assistant", content="Message 4")

        self.assertEqual(len(self.memory.get_last_n_messages(0)), 0)
        
        last_2 = self.memory.get_last_n_messages(2)
        self.assertEqual(len(last_2), 2)
        self.assertEqual(last_2[0]["content"], "Message 3")
        self.assertEqual(last_2[1]["content"], "Message 4")

        last_3 = self.memory.get_last_n_messages(3)
        self.assertEqual(len(last_3), 3)
        self.assertEqual(last_3[0]["content"], "Message 2")

        last_4 = self.memory.get_last_n_messages(4)
        self.assertEqual(len(last_4), 4)
        self.assertEqual(last_4[0]["content"], "Message 1")

        # N greater than total messages
        last_10 = self.memory.get_last_n_messages(10)
        self.assertEqual(len(last_10), 4)
        self.assertEqual(last_10[0]["content"], "Message 1")
        
        # Ensure original list is returned
        retrieved_messages = self.memory.get_last_n_messages(2)
        retrieved_messages.append({"role": "system", "content": "mutated"})
        self.assertEqual(len(self.memory.get_last_n_messages(2)), 2)


    def test_clear(self):
        self.memory.add(role="user", content="Hello")
        self.memory.add(role="assistant", content="Hi")
        self.assertNotEqual(len(self.memory.get_messages()), 0)
        self.memory.clear()
        self.assertEqual(len(self.memory.get_messages()), 0)

    def test_load_save_are_no_ops(self):
        # These methods are implemented as pass in InMemoryMemory
        # Just call them to ensure no exceptions are raised.
        try:
            self.memory.load()
            self.memory.save()
        except Exception as e:
            self.fail(f"InMemoryMemory load() or save() raised an exception: {e}")


class TestFileStorageMemory(unittest.TestCase):
    def setUp(self):
        # Create a temporary directory for test files
        self.test_dir = tempfile.mkdtemp()
        self.memory_file_path = Path(self.test_dir) / "test_memory.json"

    def tearDown(self):
        # Remove the temporary directory and its contents
        shutil.rmtree(self.test_dir)

    def test_add_and_save_persists(self):
        # Instance 1: Add messages
        memory1 = FileStorageMemory(self.memory_file_path)
        memory1.add(role="user", content="Persistence test 1")
        memory1.add(role="assistant", content="Acknowledged 1", metadata={"id": 1})
        # add() calls save() automatically

        # Instance 2: Load from the same file
        memory2 = FileStorageMemory(self.memory_file_path)
        # load() is called in __init__ if file exists
        messages = memory2.get_messages()
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["content"], "Persistence test 1")
        self.assertEqual(messages[1]["metadata"], {"id": 1})

    def test_load_from_existing_file(self):
        # Manually create a JSON file
        manual_messages = [
            {"role": "system", "content": "System init"},
            {"role": "user", "content": "Manual load test"},
        ]
        with open(self.memory_file_path, "w", encoding="utf-8") as f:
            json.dump(manual_messages, f)

        memory = FileStorageMemory(self.memory_file_path)
        # load() called in __init__
        messages = memory.get_messages()
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1]["content"], "Manual load test")

    def test_load_non_existent_file(self):
        # Ensure file does not exist before creating memory instance
        if self.memory_file_path.exists():
            os.remove(self.memory_file_path)

        memory = FileStorageMemory(self.memory_file_path)
        # load() called in __init__, should be empty if file not found
        self.assertEqual(len(memory.get_messages()), 0)

    def test_load_corrupted_file(self):
        # Create a corrupted JSON file
        with open(self.memory_file_path, "w", encoding="utf-8") as f:
            f.write("this is not valid json")

        memory = FileStorageMemory(self.memory_file_path)
        # load() should handle JSONDecodeError and start with empty memory
        self.assertEqual(len(memory.get_messages()), 0)
        
        # Also test with an empty file (which is also invalid JSON for a list)
        with open(self.memory_file_path, "w", encoding="utf-8") as f:
            f.write("")
        memory2 = FileStorageMemory(self.memory_file_path)
        self.assertEqual(len(memory2.get_messages()), 0)


    def test_clear_persists(self):
        # Add messages, they get saved
        memory1 = FileStorageMemory(self.memory_file_path)
        memory1.add(role="user", content="To be cleared")
        self.assertTrue(self.memory_file_path.exists())
        self.assertNotEqual(os.path.getsize(self.memory_file_path), 0)


        # Clear the memory
        memory1.clear() # clear() also calls save()
        self.assertEqual(len(memory1.get_messages()), 0)
        
        # Ensure the file reflects the cleared state (empty list)
        with open(self.memory_file_path, "r", encoding="utf-8") as f:
            cleared_content = json.load(f)
        self.assertEqual(cleared_content, [])

        # Create a new instance, load, and verify it's empty
        memory2 = FileStorageMemory(self.memory_file_path)
        self.assertEqual(len(memory2.get_messages()), 0)

    def test_directory_creation(self):
        deep_dir_path = Path(self.test_dir) / "sub" / "deep" / "memory.json"
        memory = FileStorageMemory(deep_dir_path)
        memory.add(role="user", content="Testing directory creation")
        # save() is called by add(). If it didn't throw error, dir was created.
        self.assertTrue(deep_dir_path.exists())
        self.assertTrue(deep_dir_path.parent.exists())
        self.assertTrue(deep_dir_path.parent.parent.exists())

    def test_get_last_n_messages_file_storage(self):
        memory = FileStorageMemory(self.memory_file_path)
        memory.add(role="user", content="Msg 1")
        memory.add(role="assistant", content="Msg 2")
        memory.add(role="user", content="Msg 3")
        memory.add(role="assistant", content="Msg 4")

        self.assertEqual(len(memory.get_last_n_messages(0)), 0)
        
        last_2 = memory.get_last_n_messages(2)
        self.assertEqual(len(last_2), 2)
        self.assertEqual(last_2[0]["content"], "Msg 3")

        # N greater than total messages
        last_10 = memory.get_last_n_messages(10)
        self.assertEqual(len(last_10), 4)
        self.assertEqual(last_10[0]["content"], "Msg 1")


# Note: Agent integration tests will require OPENAI_API_KEY
# and will make actual API calls.
# Consider using unittest.skipIf to skip these if the key is not set.
OPENAI_API_KEY_SET = os.getenv("OPENAI_API_KEY") is not None

@unittest.skipUnless(OPENAI_API_KEY_SET, "OPENAI_API_KEY is not set, skipping agent integration tests")
class TestAgentWithFileMemory(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.memory_file_path = Path(self.test_dir) / "agent_test_memory.json"

    async def asyncTearDown(self):
        shutil.rmtree(self.test_dir)

    async def test_agent_remembers_across_runs(self):
        # Agent 1: Provide information
        mem1 = FileStorageMemory(self.memory_file_path)
        agent1 = Agent(
            name="MemoryTestAgent1",
            instructions="You are a helpful assistant. My favorite food is lasagna.",
            memory=mem1,
        )
        # Initial interaction to set the information - we ask a generic question to trigger the instructions.
        await Runner.run(agent1, "Hello there.") 
        # The instructions themselves aren't directly saved as a "user" message in memory by this interaction.
        # Let's explicitly tell the agent the information.
        
        agent1_explicit = Agent(
            name="MemoryTestAgent1Explicit",
            instructions="Remember this information.",
            memory=mem1 # Use same memory object
        )
        await Runner.run(agent1_explicit, "My favorite food is lasagna.")


        # Agent 2: Recall information using a new agent instance with the same memory file
        mem2 = FileStorageMemory(self.memory_file_path) # New memory instance, same file
        agent2 = Agent(
            name="MemoryTestAgent2",
            instructions="You are a helpful assistant trying to recall information.",
            memory=mem2,
        )
        response = await Runner.run(agent2, "What is my favorite food?")
        
        final_output = str(response.final_output).lower()
        print(f"Recall Test (lasagna): Agent response: '{final_output}'")
        self.assertIn("lasagna", final_output, 
                      f"Agent should have remembered 'lasagna'. Response: {final_output}")

    async def test_agent_forgets_after_clear(self):
        # Agent 1: Provide information
        mem1 = FileStorageMemory(self.memory_file_path)
        agent1 = Agent(
            name="MemoryClearAgent1",
            instructions="Remember this fact carefully.",
            memory=mem1,
        )
        await Runner.run(agent1, "The secret code is 'X Y Z'.")

        # Agent 2: Recall information
        mem2 = FileStorageMemory(self.memory_file_path)
        agent2 = Agent(
            name="MemoryClearAgent2",
            instructions="Recall the fact.",
            memory=mem2,
        )
        response_before_clear = await Runner.run(agent2, "What is the secret code?")
        final_output_before = str(response_before_clear.final_output).lower()
        print(f"Recall Test Before Clear (XYZ): Agent response: '{final_output_before}'")
        self.assertTrue("x y z" in final_output_before or "xyz" in final_output_before,
                        f"Agent should have remembered 'X Y Z' before clear. Response: {final_output_before}")

        # Clear memory of agent2 (which uses mem2, pointing to the same file)
        agent2.memory.clear() # This clears mem2 and saves the empty state to the file

        # Agent 3: Try to recall after clear
        mem3 = FileStorageMemory(self.memory_file_path) # New memory instance, same (now cleared) file
        agent3 = Agent(
            name="MemoryClearAgent3",
            instructions="Try to recall the fact again.",
            memory=mem3,
        )
        response_after_clear = await Runner.run(agent3, "What is the secret code?")
        final_output_after = str(response_after_clear.final_output).lower()
        print(f"Recall Test After Clear (XYZ): Agent response: '{final_output_after}'")
        self.assertNotIn("x y z", final_output_after,
                         f"Agent should NOT have remembered 'X Y Z' after clear. Response: {final_output_after}")
        self.assertNotIn("xyz", final_output_after,
                         f"Agent should NOT have remembered 'xyz' after clear. Response: {final_output_after}")


if __name__ == '__main__':
    unittest.main()
