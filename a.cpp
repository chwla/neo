class Solution {
public:
    unordered_set<int> longestConsecutive(vector<int>& nums) {
        unordered_set<int> us;
        int cnt = 1, m = 1;
        for(int i=0; i<nums.size(); i++){
            us.insert(nums[i]);
        }

        return us;
    }
};