import axios from "axios";

export async function callPythonAPI(userQuery) {
  try {
    console.log("Calling Python API with query:", userQuery);
    if (!userQuery) {
      console.log("Missing query parameter");
      throw new Error("Missing query");
    }

    const response = await axios.post("http://localhost:5001/v1/chat/prompt/response", {
      query: userQuery,
    });

    return response.data;
  } catch (error) {
    console.error("Error calling Python API:", error.message);
    throw error;
  }
}

export async function configPythonAPI(collection) {
  try {
    if (!collection) {
      throw new Error("Missing collection in request body");
    }

    const response = await axios.post("http://localhost:5001/v1/chat/prompt/ai_config", {
      collections: collection,
    });

    return response.data;
  } catch (error) {
    console.error("Error sending collection:", error.message, "from Python API");
    throw error;
  }
}
